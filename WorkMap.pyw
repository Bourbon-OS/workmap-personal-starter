"""Open this kit's viewer without relying on a development checkout."""
from workmap import main

if __name__ == '__main__':
    result = main(['gui'])
    if result:
        from tkinter import messagebox
        messagebox.showerror('Work Map', '起動できませんでした。READMEの初期設定と、PowerShellからの python workmap.py gui を確認してください。')
    raise SystemExit(result)
