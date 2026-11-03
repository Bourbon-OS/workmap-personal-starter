"""A small human registration form using the normal add_work contract."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox

from pma.workmap_add_editor import prepare_add, save_add, inspect_add_result
from pma.workmap_contract import validate_snapshot
from pma.workmap_core import TransitionError
from pma.workmap_editor_ui import EditDialog, ContextHelp
from pma.workmap_sqlite_store import SQLiteStore
from pma.workmap_store import StoreError
from pma.workmap_viewer_ui import PAPER, INK, MUTED, ACCENT, actor_label


class AddWorkDialog(EditDialog):
    # Reuse the existing text scrolling, source picker, identity picker and Map
    # identity check; no alternate registration, privilege or evidence Store.
    def __init__(self, app, parent_key=None):
        tk.Toplevel.__init__(self, app.root)
        self.app = app
        self.source = copy.deepcopy(app.sources[app.current])
        self.snapshot = copy.deepcopy(app.data['snapshot'])
        self.prepared = None; self.last_result = None; self.refs = []
        self.saving = False
        self.title('Workを追加'); self.configure(bg=PAPER)
        self.geometry('900x830'); self.minsize(760, 760); self.transient(app.root)
        style = ttk.Style(self)
        style.configure('Editor.TButton', padding=(12, 6), borderwidth=1, relief='raised')
        style.configure('Primary.Editor.TButton', background=ACCENT, foreground='#ffffff')
        style.map('Primary.Editor.TButton', background=[('disabled', '#e4e1da'), ('active', '#995438')],
                  foreground=[('disabled', '#99958b'), ('!disabled', '#ffffff')])
        body = tk.Frame(self, bg=PAPER, padx=26, pady=18); body.pack(fill='both', expand=True)
        tk.Label(body, text='新しいWorkを追加', bg=PAPER, fg=INK,
                 font=('Yu Gothic UI', 17, 'bold'), anchor='w').pack(fill='x')
        tk.Label(body, text='保存先：' + str(self.source.path), bg=PAPER, fg=MUTED,
                 anchor='w', wraplength=820, justify='left').pack(fill='x', pady=(4, 10))
        self.actor_id = app.operator_id or ''
        self.actor_labels = {self.actor_id: actor_label(self.actor_id, app.actor_names)} if self.actor_id else {}
        self.actor = tk.StringVar(value=self.actor_labels.get(self.actor_id, '未設定'))
        row = tk.Frame(body, bg=PAPER); row.pack(fill='x')
        self.editor_caption = tk.Label(row, text='登録者', bg=PAPER, fg=MUTED); self.editor_caption.pack(side='left')
        self.editor_name = ttk.Button(row, textvariable=self.actor, style='Editor.TButton', command=self.change_editor)
        self.editor_name.pack(side='left', padx=10)
        self.editor_help = ContextHelp(self.editor_name, lambda:
            '追加の履歴に記録する人です。Workの担当やPinとは別です。\n'
            '記名だけでは許可を与えません。登録者ID：' + (self.actor_id or '未設定'), related=(self.editor_caption,))
        tk.Label(body, text='親Work', bg=PAPER, fg=INK, anchor='w').pack(fill='x', pady=(12, 2))
        self.parent_input = ttk.Combobox(body, state='readonly', font=('Yu Gothic UI', 10))
        self.parent_input.pack(fill='x')
        self.set_parents(parent_key)
        self.name_text = self.text_field(body, '名前', height=2)
        self.intent_text = self.text_field(body, '目的（なぜこのWorkが必要か）', height=4)
        self.origin_text = self.text_field(body, '生まれた理由（何をきっかけに生まれたか）', height=4)
        evidence = tk.Frame(body, bg=PAPER); evidence.pack(fill='x', pady=(10, 0))
        tk.Label(evidence, text='根拠資料', bg=PAPER, fg=INK).pack(side='left')
        self.choose_button = ttk.Button(evidence, text='資料を選ぶ', style='Editor.TButton', command=self.choose_sources)
        self.choose_button.pack(side='right')
        self.source_text = tk.Label(body, text='未選択', bg=PAPER, fg=MUTED, anchor='w', justify='left', wraplength=820)
        self.source_text.pack(fill='x', pady=(4, 6))
        self.message = tk.StringVar(value='親・名前・目的・生まれた理由・根拠を入力して「追加内容を確認」へ。')
        tk.Label(body, textvariable=self.message, bg=PAPER, fg=ACCENT, anchor='w', justify='left', wraplength=820).pack(fill='x', pady=6)
        self.preview_text = tk.Label(body, text='', bg=PAPER, fg=MUTED, anchor='w', justify='left', wraplength=820)
        self.preview_text.pack(fill='x')
        buttons = tk.Frame(body, bg=PAPER); buttons.pack(side='bottom', fill='x', pady=(10, 0))
        ttk.Button(buttons, text='閉じる', style='Editor.TButton', command=self.destroy).pack(side='left')
        self.back_button = ttk.Button(buttons, text='戻って修正', style='Editor.TButton', command=self.unlock, state='disabled')
        self.back_button.pack(side='left', padx=8)
        self.reload_button = ttk.Button(buttons, text='最新Mapを取得', style='Editor.TButton', command=self.reload_snapshot)
        self.reload_button.pack(side='left')
        self.check_result_button = ttk.Button(buttons, text='追加結果を確認', style='Editor.TButton', command=self.check_result, state='disabled')
        self.check_result_button.pack(side='left', padx=8)
        self.save_button = ttk.Button(buttons, text='正本へ追加', style='Primary.Editor.TButton', command=self.save, state='disabled')
        self.save_button.pack(side='right')
        self.preview_button = ttk.Button(buttons, text='追加内容を確認', style='Editor.TButton', command=self.preview)
        self.preview_button.pack(side='right', padx=8)
        pending = getattr(app, 'pending_additions', {}).get(str(self.source.path.resolve()))
        if pending is not None:
            self.prepared = pending
            change = pending.request['changes'][0]; work = change['value']['work']
            self.set_parents(change['target'])
            for widget, value in ((self.name_text, work['name']), (self.intent_text, work['intent']),
                                  (self.origin_text, work['origin']['summary'])):
                widget.insert('1.0', value)
            self.actor_id = pending.request['actor_id']; self.actor.set(actor_label(self.actor_id, app.actor_names))
            self.source_text.configure(text='\n'.join(b['ref'] for b in pending.bindings))
            self.lock(); self.reload_button.configure(state='disabled'); self.check_result_button.configure(state='normal')
            self.message.set('前の追加結果が未確認です。新しく登録せず、同じ要求の結果を確認してください。')
        self.grab_set(); self.name_text.focus_set()

    def set_parents(self, selected):
        self.parent_keys = list(self.snapshot['map']['members'])
        self.parent_input.configure(values=[self.snapshot['display_ids'][k] + '  ' + self.snapshot['works'][k]['name'] for k in self.parent_keys])
        if selected in self.parent_keys:
            self.parent_input.current(self.parent_keys.index(selected))
        else:
            self.parent_input.set('親Workを選んでください')

    def preview(self):
        if self.prepared is not None: return
        try:
            index = self.parent_input.current()
            parent = self.parent_keys[index] if index >= 0 else ''
            self.prepared = prepare_add(self.source, self.snapshot, parent=parent,
                name=self.name_text.get('1.0', 'end-1c'), intent=self.intent_text.get('1.0', 'end-1c'),
                origin=self.origin_text.get('1.0', 'end-1c'), actor=self.actor_id, refs=self.refs)
            self.lock(); self.save_button.configure(state='normal'); self.back_button.configure(state='normal')
            value = self.prepared.request['changes'][0]['value']
            self.preview_text.configure(text='追加予定：' + value['display_id'] + '　親：' + self.snapshot['display_ids'][parent])
            self.message.set('上の内容を確認してください。まだ追加していません。担当・Pin・実行許可は追加しません。')
        except (OSError, ValueError, KeyError, TransitionError, StoreError) as exc:
            self.prepared = None; self.save_button.configure(state='disabled'); self.message.set(str(exc))

    def lock(self):
        for w in (self.editor_name, self.parent_input, self.name_text, self.intent_text, self.origin_text,
                  self.choose_button, self.preview_button):
            w.configure(state='disabled')

    def unlock(self):
        if self.prepared and (self.prepared.request_path or self.last_result and self.last_result['status'] == 'unknown'):
            self.message.set('追加要求は変更せず、同じ要求の結果を確認してください。'); return
        self.prepared = None; self.last_result = None
        for w in (self.editor_name, self.name_text, self.intent_text, self.origin_text, self.choose_button, self.preview_button):
            w.configure(state='normal')
        self.parent_input.configure(state='readonly'); self.save_button.configure(state='disabled')
        self.back_button.configure(state='disabled'); self.preview_text.configure(text='')
        self.message.set('入力を修正し、追加内容をもう一度確認してください。')

    def reload_snapshot(self):
        if not self.still_same_map():
            self.message.set('表示中のMapが変わりました。対象を確認し直してください。'); return
        if self.prepared and (self.prepared.request_path or self.last_result and self.last_result['status'] == 'unknown'):
            self.message.set('送信した追加要求は差し替えません。「追加結果を確認」を使ってください。'); return
        try:
            selected = self.parent_keys[self.parent_input.current()] if self.parent_input.current() >= 0 else None
            state = validate_snapshot(SQLiteStore(self.source.path).read(read_only=True).snapshot)
            if state['project_id'] != self.snapshot['project_id'] or state['metadata'].get('store_role') != 'canonical_current':
                raise ValueError('保存先のMapが変わりました。')
            self.snapshot = state; self.unlock(); self.set_parents(selected)
            self.message.set('入力を残して最新Mapを取得しました。親と内容を再確認してください。')
        except (OSError, ValueError, TransitionError, StoreError) as exc:
            self.message.set('最新Mapを取得できません。' + str(exc))

    def save(self):
        if self.saving or self.prepared is None or self.last_result is not None: return
        self.saving = True
        try:
            self.submit_confirmed()
        finally:
            self.saving = False

    def submit_confirmed(self):
        if not self.still_same_map():
            self.message.set('表示中のMapが変わりました。追加せず対象を確認し直してください。'); return
        self.save_button.configure(state='disabled')
        change = self.prepared.request['changes'][0]; work = change['value']['work']
        confirmation = ('追加：' + change['value']['display_id'] + '  ' + work['name'][:180]
            + '\n親：' + self.snapshot['display_ids'][change['target']] + '  ' + self.snapshot['works'][change['target']]['name']
            + '\n登録者：' + self.prepared.request['actor_id'] + '\n保存先：' + str(self.source.path)
            + '\n\n目的・生まれた理由・根拠は、この確認画面に表示した内容で登録します。\n本当に正本へ追加してよいですか？')
        try:
            approved = messagebox.askyesno('Workの追加を確認', confirmation, parent=self,
                                          default=messagebox.NO, icon=messagebox.QUESTION)
        except tk.TclError:
            self.save_button.configure(state='normal'); self.message.set('確認を開けなかったため、追加しませんでした。'); return
        if approved is not True:
            self.save_button.configure(state='normal'); self.message.set('今回は追加していません。'); return
        if not self.still_same_map():
            self.message.set('確認中に表示するMapが変わりました。追加していません。'); return
        self.back_button.configure(state='disabled'); self.update_idletasks()
        self.last_result = save_add(self.prepared); self.message.set(self.last_result['message'])
        if not hasattr(self.app, 'pending_additions'): self.app.pending_additions = {}
        if self.last_result['status'] == 'unknown':
            self.app.pending_additions[str(self.source.path.resolve())] = self.prepared
        if self.prepared.request_path or self.last_result['status'] == 'unknown':
            self.reload_button.configure(state='disabled'); self.check_result_button.configure(state='normal')
        elif self.last_result['status'] == 'rejected':
            self.back_button.configure(state='normal')
        self.after_result()

    def after_result(self):
        if self.last_result['status'] != 'unknown':
            getattr(self.app, 'pending_additions', {}).pop(str(self.source.path.resolve()), None)
        if self.last_result['status'] in ('confirmed', 'saved_but_changed') and self.still_same_map():
            self.app.operator_id = self.prepared.request['actor_id']
            self.app.refresh(select_key=self.last_result['work_key'])

    def check_result(self):
        if self.prepared is None: return
        try:
            self.last_result = inspect_add_result(self.prepared); self.message.set(self.last_result['message']); self.after_result()
        except (OSError, ValueError, RuntimeError, TransitionError, StoreError) as exc:
            self.message.set('追加結果は未確認です。正本を復旧・再読して同じ要求を確認してください。' + str(exc))
