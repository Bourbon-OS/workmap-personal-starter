"""Human review form. Calls the existing Core adapter, never writes raw Map data."""
import copy
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from pma.workmap_editor import FIELDS, KINDS, prepare_edit, save_edit, inspect_edit_result, source_ref
from pma.workmap_viewer_ui import PAPER, INK, MUTED, ACCENT, actor_label
from pma.workmap_core import TransitionError
from pma.workmap_store import StoreError


REASON_SUGGESTIONS = {
    'name': ('誤字・表記を訂正する', 'Workの内容が伝わる名前にする', '表記を統一する'),
    'intent': ('元の意図と異なる記録を訂正する', '同じ目的をより明確にする', '未確認だった目的をOwner判断で確定する'),
    'origin': ('原資料と異なる記録を訂正する', '生まれた経緯の確認できた事実を補足する', '出典を確認して記録を正確にする'),
}


class ContextHelp:
    """Optional help, reachable by pointer, keyboard focus, or click."""
    def __init__(self, widget, text, related=()):
        self.widget = widget; self.text = text; self.popup = None; self.pending = None
        for target in (widget, *related):
            target.bind('<Enter>', self.schedule)
            target.bind('<Leave>', self.hide)
            target.bind('<FocusIn>', self.show)
            target.bind('<FocusOut>', self.hide)
            target.bind('<Escape>', self.hide)
            target.bind('<Destroy>', self.hide)
        widget.winfo_toplevel().bind('<Escape>', self.hide, add='+')

    def schedule(self, event=None):
        self.hide()
        self.pending = self.widget.after(400, self.show)

    def show(self, event=None):
        self.hide()
        popup = self.popup = tk.Toplevel(self.widget)
        popup.withdraw(); popup.overrideredirect(True)
        tk.Label(popup, text=self.text(), bg='#fffbe7', fg=INK, padx=12, pady=9,
                 justify='left', wraplength=390, font=('Yu Gothic UI', 9),
                 relief='solid', borderwidth=1).pack()
        popup.update_idletasks()
        x = min(self.widget.winfo_rootx(), popup.winfo_screenwidth() - popup.winfo_reqwidth() - 8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + popup.winfo_reqheight() > popup.winfo_screenheight():
            y = self.widget.winfo_rooty() - popup.winfo_reqheight() - 6
        popup.geometry(f'+{max(0, x)}+{max(0, y)}'); popup.deiconify()

    def hide(self, event=None):
        if self.pending is not None:
            self.widget.after_cancel(self.pending); self.pending = None
        if self.popup is not None:
            self.popup.destroy(); self.popup = None


class EditDialog(tk.Toplevel):
    def __init__(self, app, key):
        super().__init__(app.root)
        self.app = app; self.key = key
        self.source = copy.deepcopy(app.sources[app.current])
        self.snapshot = copy.deepcopy(app.data['snapshot'])
        self.prepared = None; self.last_result = None; self.refs = []
        self.reason_drafts = {}; self.loaded_field = None
        self.title('Workを訂正・編集'); self.configure(bg=PAPER)
        self.geometry('900x890'); self.minsize(740, 790); self.transient(app.root)
        style = ttk.Style(self)
        style.configure('Editor.TButton', padding=(12, 6), background='#ffffff',
                        foreground=INK, borderwidth=1, relief='raised', bordercolor='#b1ada3')
        style.map('Editor.TButton', background=[('disabled', '#efede7'), ('pressed', '#d5d1c7'), ('active', '#e6e3dc')],
                  foreground=[('disabled', '#99958b')], bordercolor=[('focus', ACCENT)])
        style.configure('Primary.Editor.TButton', background=ACCENT, foreground='#ffffff', bordercolor=ACCENT)
        style.map('Primary.Editor.TButton', background=[('disabled', '#e4e1da'), ('pressed', '#723b27'), ('active', '#995438')],
                  foreground=[('disabled', '#99958b'), ('!disabled', '#ffffff')])
        body = tk.Frame(self, bg=PAPER, padx=26, pady=18); body.pack(fill='both', expand=True)
        work = self.snapshot['works'][key]
        tk.Label(body, text=self.snapshot['display_ids'][key] + '  ' + work['name'],
                 bg=PAPER, fg=INK, font=('Yu Gothic UI', 14, 'bold'), anchor='w',
                 wraplength=820, justify='left').pack(fill='x')
        tk.Label(body, text='保存先：' + str(self.source.path), bg=PAPER, fg=MUTED,
                 anchor='w', wraplength=820, justify='left').pack(fill='x', pady=(4, 10))
        parent = next((r['parent'] for r in self.snapshot['map']['relations'] if r['kind'] == 'parent' and r['child'] == key), None)
        parent_text = self.snapshot['display_ids'][parent] + '  ' + self.snapshot['works'][parent]['name'] if parent else 'なし'
        tk.Label(body, text='親Work：' + parent_text, bg=PAPER, fg=MUTED, anchor='w',
                 wraplength=820, justify='left').pack(fill='x', pady=(0, 6))
        self.actor_id = app.operator_id or ''
        self.actor_labels = {self.actor_id: actor_label(self.actor_id, app.actor_names)} if self.actor_id else {}
        initial = self.actor_labels.get(self.actor_id, '未設定')
        self.actor = tk.StringVar(value=initial); self.field = tk.StringVar(value='名前')
        row = tk.Frame(body, bg=PAPER); row.pack(fill='x')
        self.editor_caption = tk.Label(row, text='編集者', bg=PAPER, fg=MUTED)
        self.editor_caption.pack(side='left')
        self.editor_name = ttk.Button(row, textvariable=self.actor, style='Editor.TButton', command=self.change_editor)
        self.editor_name.pack(side='left', padx=(10, 12))
        self.editor_help = ContextHelp(self.editor_name, lambda:
            'この変更を履歴に記録する人です。Workの担当やPinとは別です。\n'
            '自己申告の記名で、本人確認や許可の付与は行いません。\n'
            '編集者ID：' + (self.actor_id or '未設定'), related=(self.editor_caption,))
        self.editor_caption.bind('<Button-1>', self.editor_help.show)
        self.field_input = ttk.Combobox(row, textvariable=self.field, values=list(FIELDS), state='readonly', width=26)
        self.field_input.pack(side='right')
        self.field_input.bind('<<ComboboxSelected>>', lambda _: self.load_field())
        self.old_text = self.text_field(body, '現在の値（読込時点）', height=3)
        self.new_text = self.text_field(body, '変更後', height=4)
        tk.Label(body, text='変更理由', bg=PAPER, fg=INK, anchor='w').pack(fill='x', pady=(4, 2))
        self.reason_input = ttk.Combobox(body, state='normal', font=('Yu Gothic UI', 10))
        self.reason_input.pack(fill='x', pady=(0, 4))
        self.evidence_row = evidence_row = tk.Frame(body, bg=PAPER); evidence_row.pack(fill='x', pady=(4, 0))
        tk.Label(evidence_row, text='根拠資料', bg=PAPER, anchor='w').pack(side='left')
        self.choose_button = ttk.Button(evidence_row, text='資料を選ぶ', style='Editor.TButton', command=self.choose_sources)
        self.choose_button.pack(side='right')
        self.source_help = ContextHelp(self.choose_button, lambda:
            ('今回のOwner判断を docs/work-management/evidence/owner_任意名.txt に保存して選びます。\n'
             '変更前後・理由・同じ目的である根拠を書いてください。\n'
             '既存の許可文書や無関係な資料を流用せず、内容は編集者が確認します。'
             if FIELDS[self.field.get()] == 'intent' else
             'このMapのdocs配下に、今回の訂正理由を確認できる資料を保存して選びます。\n'
             'ファイルの存在や版の一致と、内容が訂正を支えることは別に確認します。'))
        self.source_text = tk.Label(body, text='未選択', bg=PAPER, fg=MUTED, anchor='w', justify='left', wraplength=820)
        self.source_text.pack(fill='x', pady=(3, 6))
        self.intent_controls = tk.Frame(body, bg=PAPER)
        self.kind = tk.StringVar(value='誤記録の訂正')
        tk.Label(self.intent_controls, text='目的の変更区分', bg=PAPER, fg=INK, anchor='w').pack(fill='x')
        self.kind_input = ttk.Combobox(self.intent_controls, textvariable=self.kind, values=list(KINDS), state='readonly', width=38)
        self.kind_input.pack(anchor='w')
        self.basis_text = self.text_field(self.intent_controls, '同じWorkで扱う理由（元の目的とのつながり）', height=2)
        self.owner_confirmed = tk.BooleanVar(value=False)
        self.owner_check = tk.Checkbutton(self.intent_controls, variable=self.owner_confirmed,
            text='資料にOwnerの採用判断があり、同じ目的のWorkとして扱えることを確認した',
            bg=PAPER, anchor='w', justify='left', wraplength=820)
        self.owner_check.pack(fill='x')
        self.message = tk.StringVar(value='変更後・理由・根拠を入力して「変更を確認」へ。')
        tk.Label(body, textvariable=self.message, bg=PAPER, fg=ACCENT, wraplength=820,
                 justify='left', anchor='w').pack(fill='x', pady=(8, 4))
        self.preview_text = tk.Label(body, text='', bg=PAPER, fg=MUTED, anchor='w', justify='left', wraplength=820)
        self.preview_text.pack(fill='x')
        buttons = tk.Frame(body, bg=PAPER); buttons.pack(side='bottom', fill='x', pady=(10, 0))
        ttk.Button(buttons, text='取り消す／閉じる', style='Editor.TButton', command=self.destroy).pack(side='left')
        self.back_button = ttk.Button(buttons, text='戻って修正', style='Editor.TButton', command=self.unlock, state='disabled')
        self.back_button.pack(side='left', padx=8)
        self.check_result_button = ttk.Button(buttons, text='保存結果を確認', style='Editor.TButton', command=self.check_result, state='disabled')
        self.check_result_button.pack(side='left')
        self.save_button = ttk.Button(buttons, text='この内容を保存', style='Primary.Editor.TButton', command=self.save, state='disabled')
        self.save_button.pack(side='right')
        self.preview_button = ttk.Button(buttons, text='変更を確認', style='Primary.Editor.TButton', command=self.preview)
        self.preview_button.pack(side='right', padx=8)
        self.load_field(); self.grab_set()

    def text_field(self, parent, title, height):
        tk.Label(parent, text=title, bg=PAPER, fg=INK, anchor='w').pack(fill='x', pady=(4, 2))
        frame = tk.Frame(parent, bg=PAPER); frame.pack(fill='x')
        frame.columnconfigure(0, weight=1)
        widget = tk.Text(frame, height=height, wrap='word', font=('Yu Gothic UI', 10),
                         bg='#ffffff', fg=INK, padx=8, pady=4)
        widget.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=widget.yview)
        scroll.grid(row=0, column=1, sticky='ns'); scroll.grid_remove()
        def update_scroll(first, last):
            scroll.set(first, last)
            if float(first) > 0 or float(last) < 1:
                if not scroll.winfo_manager(): scroll.grid()
            elif scroll.winfo_manager():
                scroll.grid_remove()
        widget.configure(yscrollcommand=update_scroll)
        pending = None
        def sync_scroll():
            nonlocal pending
            pending = None
            # Wrapped-line metrics are lazy; refresh them before checking fit.
            widget.count('1.0', 'end', 'update', 'displaylines')
            update_scroll(*widget.yview())
        def schedule_sync(event=None):
            nonlocal pending
            if pending is None: pending = widget.after_idle(sync_scroll)
        def modified(event=None):
            if widget.edit_modified():
                widget.edit_modified(False); schedule_sync()
        def dispose(event=None):
            if pending is not None: widget.after_cancel(pending)
        widget.bind('<<Modified>>', modified)
        widget.bind('<Configure>', schedule_sync)
        widget.bind('<Destroy>', dispose)
        widget.vertical_scrollbar = scroll
        return widget

    def change_editor(self):
        if self.prepared is not None: return
        self.editor_help.hide()
        value = simpledialog.askstring('編集者を変更', '編集者ID',
                                       initialvalue=self.actor_id, parent=self)
        if value is None or not value.strip(): return
        self.actor_id = value.strip()
        label = actor_label(self.actor_id, self.app.actor_names)
        self.actor_labels[self.actor_id] = label; self.actor.set(label)

    def load_field(self):
        self.prepared = None; self.save_button.configure(state='disabled')
        field = FIELDS[self.field.get()]; value = self.snapshot['works'][self.key][field]
        if self.loaded_field is not None:
            self.reason_drafts[self.loaded_field] = self.reason_input.get()
        self.reason_input.set(self.reason_drafts.get(field, ''))
        self.loaded_field = field
        self.reason_input.configure(values=REASON_SUGGESTIONS[field])
        if field == 'origin': value = value['summary']
        self.old_text.configure(state='normal'); self.old_text.delete('1.0', 'end')
        self.old_text.insert('1.0', value if value is not None else '未確認'); self.old_text.configure(state='disabled')
        self.new_text.configure(state='normal'); self.new_text.delete('1.0', 'end'); self.new_text.insert('1.0', value or '')
        self.owner_confirmed.set(False)
        if field == 'intent':
            self.kind.set('未確認目的の確定' if value is None else '誤記録の訂正')
            self.intent_controls.pack(fill='x', before=self.evidence_row, pady=(4, 0))
        else:
            self.intent_controls.pack_forget()

    def choose_sources(self):
        files = filedialog.askopenfilenames(parent=self, title='このMapの根拠資料を選ぶ',
            initialdir=str(self.source.evidence_root / 'docs'),
            filetypes=[('根拠文書', '*.txt *.md *.json')])
        if not files: return
        try:
            refs = [source_ref(self.source.evidence_root, f) for f in files]
            self.refs = list(dict.fromkeys(refs)); self.source_text.configure(text='\n'.join(self.refs))
        except ValueError as exc:
            self.message.set(str(exc))

    def preview(self):
        try:
            self.prepared = prepare_edit(self.source, self.snapshot, self.key,
                field=FIELDS[self.field.get()], value=self.new_text.get('1.0', 'end-1c'),
                actor=self.actor_id,
                reason=self.reason_input.get(), refs=self.refs,
                kind=KINDS[self.kind.get()], same_work_basis=self.basis_text.get('1.0', 'end-1c'),
                owner_reviewed=self.owner_confirmed.get())
            self.lock(); self.save_button.configure(state='normal'); self.back_button.configure(state='normal')
            editor_id = self.prepared.request['actor_id']
            self.preview_text.configure(text='保存対象：' + self.snapshot['display_ids'][self.key]
                + '　編集者：' + self.actor_labels.get(editor_id, editor_id))
            self.message.set('上の現在値・変更後・理由・根拠を確認してください。まだ保存していません。')
        except (ValueError, OSError, KeyError, TransitionError, StoreError) as exc:
            self.prepared = None; self.save_button.configure(state='disabled'); self.message.set(str(exc))

    def lock(self):
        for widget in (self.editor_name, self.field_input, self.kind_input, self.owner_check,
                       self.choose_button, self.new_text, self.reason_input, self.basis_text, self.preview_button):
            widget.configure(state='disabled')

    def unlock(self):
        if self.prepared and self.prepared.request_path:
            self.message.set('保存要求を送った後は、この画面で内容を差し替えません。結果を確認して閉じ、正本を読み直してください。')
            return
        self.prepared = None; self.save_button.configure(state='disabled'); self.back_button.configure(state='disabled')
        for widget in (self.editor_name, self.owner_check, self.choose_button, self.new_text,
                       self.reason_input, self.basis_text, self.preview_button): widget.configure(state='normal')
        for widget in (self.field_input, self.kind_input): widget.configure(state='readonly')
        self.message.set('変更内容を修正し、もう一度確認してください。'); self.preview_text.configure(text='')

    def still_same_map(self):
        return (self.app.data is not None and self.app.current >= 0
                and self.app.sources[self.app.current].path.resolve() == self.source.path.resolve()
                and self.app.sources[self.app.current].evidence_root.resolve() == self.source.evidence_root.resolve()
                and self.app.data['snapshot']['project_id'] == self.snapshot['project_id'])

    def save(self):
        if self.prepared is None: return
        if not self.still_same_map():
            self.message.set('表示中のMapが変わりました。編集画面を閉じて対象を確認し直してください。')
            self.save_button.configure(state='disabled'); return
        self.save_button.configure(state='disabled')
        def shown(value):
            text = value.get('summary') if isinstance(value, dict) else value
            text = '未確認' if text is None else str(text)
            return text if len(text) <= 240 else text[:240] + '\n…（全文は編集画面で確認してください）'
        field_label = next(label for label, value in FIELDS.items() if value == self.prepared.field)
        editor_id = self.prepared.request['actor_id']
        confirmation = ('対象：' + self.snapshot['display_ids'][self.key] + '  ' + self.snapshot['works'][self.key]['name']
            + '\n項目：' + field_label
            + '\n編集者：' + self.actor_labels.get(editor_id, editor_id) + '\n編集者ID：' + editor_id
            + '\n保存先：' + str(self.source.path)
            + '\n\n変更前：\n' + shown(self.prepared.old_value)
            + '\n\n変更後：\n' + shown(self.prepared.new_value)
            + '\n\n本当にこの内容を正本へ反映してよいですか？')
        try:
            approved = messagebox.askyesno('変更の反映を確認', confirmation,
                parent=self, default=messagebox.NO, icon=messagebox.QUESTION)
        except tk.TclError:
            self.save_button.configure(state='normal')
            self.message.set('反映確認を開けなかったため、保存しませんでした。'); return
        if approved is not True:
            self.save_button.configure(state='normal')
            self.message.set('今回は反映せず、編集画面へ戻りました。'); return
        if not self.still_same_map():
            self.message.set('確認中に表示するMapが変わりました。対象を確認し直してください。'); return
        self.back_button.configure(state='disabled'); self.update_idletasks()
        self.last_result = save_edit(self.prepared)
        self.message.set(self.last_result['message'] + ('\n' + self.last_result['detail']
                        if self.last_result.get('detail') else ''))
        if self.last_result['status'] in ('confirmed', 'saved_but_changed'):
            self.app.operator_id = self.prepared.request['actor_id']
            self.app.refresh()
        if self.prepared.request_path or self.last_result['status'] == 'unknown':
            self.check_result_button.configure(state='normal')

    def check_result(self):
        if not self.prepared: return
        try:
            self.last_result = inspect_edit_result(self.prepared); self.message.set(self.last_result['message'])
            if self.still_same_map() and self.last_result['status'] in ('confirmed', 'saved_but_changed'):
                self.app.refresh()
        except (OSError, ValueError, RuntimeError, TransitionError, StoreError) as exc:
            self.message.set('結果は未確認です。正本を読み直してください。' + str(exc))
