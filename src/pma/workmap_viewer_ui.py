"""Presentation widgets only. No Store, identity registry, or network access."""
import tkinter as tk
from tkinter import ttk

PAPER = '#f6f4ef'
INK = '#242b30'
MUTED = '#626b70'
DARK = '#262d32'
ACCENT = '#a95130'
LINE = '#deded7'


def actor_label(actor, actor_names=None):
    names = actor_names or {}
    if actor in names:
        name = names[actor]
        return name + (' · ' + actor if list(names.values()).count(name) > 1 else '')
    if actor.startswith('actor:codex-task:'):
        # Keep the full task suffix: two actors must never get one label.
        return 'Codex · ' + actor.removeprefix('actor:codex-task:')
    if actor.startswith('actor:subagent:'):
        return 'AI · ' + actor.removeprefix('actor:subagent:')
    if actor.startswith('actor:codex:'):
        return 'チャット名未取得 · ' + actor.removeprefix('actor:codex:')
    return actor


def actor_lines(pins, actor_names=None):
    return ['{}  —  {}'.format({'active': '作業中', 'held': '保持中'}.get(p['mode'], p['mode']),
                              actor_label(p['actor_id'], actor_names)) for p in pins]


def pin_summary(pins):
    """Count distinct actors per mode, without choosing one representative Pin."""
    return '   '.join(f'{symbol} {len(actors)}' for mode, symbol in [('active', '▶'), ('held', '⏸')]
                      if (actors := {p['actor_id'] for p in pins if p['mode'] == mode})) or '—'


def people_groups(pins, actor_names=None):
    labels = {p['actor_id']: actor_label(p['actor_id'], actor_names) for p in pins}
    collisions = {label for label in labels.values() if list(labels.values()).count(label) > 1}
    # Include unnamed actors in collision detection, across both modes.
    # Put the literal ID on a separate labelled line so it is not another name.
    if collisions:
        labels = {actor: label + '\n    担当ID：' + actor for actor, label in labels.items()}
    groups = []
    for mode, title in [('active', '▶ 作業中 / Active'), ('held', '⏸ 保持中 / Held')]:
        actors = list(dict.fromkeys(p['actor_id'] for p in pins if p['mode'] == mode))
        if actors:
            groups.append(f'{title}（{len(actors)}）\n' + '\n'.join('・' + labels[a] for a in actors))
    return '\n\n'.join(groups) or '現在の担当の記録はありません。'


def actor_code(index):
    """Window-local shorthand, not a new identity or persistent assignment."""
    code = ''; index += 1
    while index:
        index, digit = divmod(index - 1, 26)
        code = chr(65 + digit) + code
    return code


def pin_badges(pins, codes):
    return '   '.join(f'{symbol} {codes[actor]}' for mode, symbol in [('active', '▶'), ('held', '⏸')]
                      for actor in dict.fromkeys(p['actor_id'] for p in pins if p['mode'] == mode)) or '—'


def actor_assignments(snapshot):
    """Current Map only; never use visible/filter results or retired history."""
    result = {}
    for pin in snapshot['map']['pins']:
        if pin['work_id'] in snapshot['map']['members']:
            modes = result.setdefault(pin['actor_id'], {'active': [], 'held': []})
            if pin['work_id'] not in modes[pin['mode']]:
                modes[pin['mode']].append(pin['work_id'])
    for modes in result.values():
        for works in modes.values():
            works.sort(key=lambda k: tuple(int(p) for p in snapshot['display_ids'][k][1:].split('.')))
    return result


def read_text(parent, content, *, large=False, muted=False):
    text = tk.Text(parent, wrap='word', height=2, borderwidth=0, highlightthickness=0,
                   bg=PAPER, fg=MUTED if muted else INK, cursor='arrow',
                   font=('Yu Gothic UI', 11 if not large else 17),
                   spacing1=2, spacing3=5, padx=0, pady=2, takefocus=True)
    text.insert('1.0', content); text.configure(state='disabled')
    text.pack(fill='x', pady=(3, 10))
    def resize(_=None):
        if text.winfo_exists() and text.winfo_width() > 20:
            count = text.count('1.0', 'end-1c', 'displaylines')
            wanted = max(1, (count[0] if count else 0) + 1)
            if int(text.cget('height')) != wanted:
                text.configure(height=wanted)
    text.bind('<Configure>', resize)
    return text


class FoldSection(tk.Frame):
    def __init__(self, parent, title, content='', *, expanded=False, warning=False, meta=None):
        super().__init__(parent, bg=PAPER)
        self.expanded = expanded; self.title = title
        tk.Frame(self, bg=LINE, height=1).pack(fill='x')
        self.button = tk.Button(self, text='', command=self.toggle, anchor='w', justify='left',
                                relief='flat', bd=0, padx=0, pady=12, bg=PAPER,
                                activebackground='#ecebe4', fg=ACCENT if warning else INK,
                                activeforeground=INK, font=('Yu Gothic UI', 11, 'bold'),
                                cursor='hand2', takefocus=True, highlightthickness=1,
                                highlightbackground=PAPER, highlightcolor=ACCENT)
        self.button.pack(fill='x')
        self.button.bind('<Configure>', lambda e: self.button.configure(wraplength=max(100, e.width - 25)))
        self.body = tk.Frame(self, bg=PAPER, padx=16)
        self.text = read_text(self.body, content)
        self.meta = None
        if meta:
            self.meta = FoldSection(self.body, '根拠・記録の詳細', meta)
            self.meta.pack(fill='x', pady=(0, 12))
        self.show()

    def show(self):
        self.button.configure(text=('−  ' if self.expanded else '＋  ') + self.title)
        if self.expanded:
            self.body.pack(fill='x')
        else:
            self.body.pack_forget()

    def toggle(self):
        self.expanded = not self.expanded; self.show()


class SectionPane(ttk.Frame):
    def __init__(self, parent, *, search_var=None):
        super().__init__(parent)
        if search_var is not None:
            searchrow = tk.Frame(self, bg=PAPER, padx=28, pady=12)
            searchrow.pack(fill='x')
            tk.Label(searchrow, text='理由を探す', bg=PAPER, fg=MUTED,
                     font=('Yu Gothic UI', 10)).pack(side='left', padx=(0, 10))
            ttk.Entry(searchrow, textvariable=search_var).pack(side='left', fill='x', expand=True)
        self.canvas = tk.Canvas(self, bg=PAPER, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        scroll.pack(side='right', fill='y'); self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.inner = tk.Frame(self.canvas, bg=PAPER, padx=28, pady=24)
        self.window = self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.inner.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.sections = {}; self.contents = []

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.sections.clear(); self.contents.clear(); self.canvas.yview_moveto(0)

    def heading(self, title, eyebrow=''):
        if eyebrow:
            tk.Label(self.inner, text=eyebrow, bg=PAPER, fg=ACCENT,
                     font=('Yu Gothic UI', 10, 'bold'), anchor='w').pack(fill='x', pady=(0, 9))
        read_text(self.inner, title, large=True); self.contents.append(title)

    def notice(self, text, *, warning=False):
        label = tk.Label(self.inner, text=text, bg='#eee4d5' if warning else PAPER,
                         fg='#794524' if warning else MUTED, justify='left', anchor='w',
                         padx=10 if warning else 0, pady=9, font=('Yu Gothic UI', 10))
        label.pack(fill='x', pady=(0, 12))
        label.bind('<Configure>', lambda e: label.configure(wraplength=max(100, e.width - 24)))
        self.contents.append(text)

    def section(self, key, title, content, *, expanded=False, warning=False, meta=None):
        section = FoldSection(self.inner, title, content, expanded=expanded, warning=warning, meta=meta)
        section.pack(fill='x'); self.sections[key] = section
        self.contents.extend([title, content, meta or '']); return section

    def get(self, *_):
        """Text represented by this pane, including explicitly foldable detail."""
        return '\n\n'.join(self.contents)

    def set_text(self, content):
        self.clear()
        if content:
            read_text(self.inner, content); self.contents.append(content)
