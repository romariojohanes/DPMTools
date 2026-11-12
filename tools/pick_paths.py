"""Native Windows file selection; returned paths only, no file contents."""
import json
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
try:
    if sys.argv[1] == 'directory':
        path = filedialog.askdirectory(title='Select CDR directory')
        selected = [path] if path else []
    else:
        selected = list(filedialog.askopenfilenames(title='Select CDR extracts', filetypes=[('CDR extracts', '*.csv *.xlsx')]))
    print(json.dumps(selected))
finally:
    root.destroy()
