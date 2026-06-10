"""
Standalone file dialog helper — runs in its own subprocess so that it
has its own Tkinter event loop and does not fight with Eel/Chrome.
Called by SeismicViewer.py via subprocess.run().

Usage:
    python _filedialog.py open         "Title"  "pattern1=*.ext|pattern2=*.*"
    python _filedialog.py save         "Title"  "default.ext"  "pattern1=*.ext|pattern2=*.*"
    python _filedialog.py folder       "Title"

Prints selected path to stdout. Empty output == cancelled.
"""
import sys
import os
import subprocess

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    print("", end='')
    sys.exit(1)


def parse_filetypes(spec):
    """Parse 'label1=*.ext|label2=*.*' -> [('label1','*.ext'), ('label2','*.*')]."""
    if not spec:
        return [("All files", "*.*")]
    out = []
    for part in spec.split('|'):
        if '=' in part:
            label, pattern = part.split('=', 1)
            out.append((label.strip(), pattern.strip()))
    return out or [("All files", "*.*")]


def activate_self_macos():
    """On macOS, bring this Python process to the foreground via osascript."""
    if sys.platform != 'darwin':
        return
    try:
        pid = str(os.getpid())
        script = (
            'tell application "System Events"\n'
            '  set frontmost of first process whose unix id is ' + pid + ' to true\n'
            'end tell'
        )
        subprocess.call(['osascript', '-e', script], timeout=2)
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    mode = sys.argv[1]

    # Bring OUR process to front BEFORE creating the Tk root
    activate_self_macos()

    # Create Tk root, keep it invisible, force it topmost
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    # On Windows this forces z-order above everything:
    try:
        root.call('wm', 'attributes', '.', '-topmost', True)
    except Exception:
        pass
    root.lift()
    root.focus_force()
    root.update()

    result = ''

    try:
        if mode == 'open':
            title  = sys.argv[2] if len(sys.argv) > 2 else 'Open'
            ftypes = parse_filetypes(sys.argv[3] if len(sys.argv) > 3 else '')
            result = filedialog.askopenfilename(parent=root, title=title, filetypes=ftypes)

        elif mode == 'save':
            title   = sys.argv[2] if len(sys.argv) > 2 else 'Save'
            initfn  = sys.argv[3] if len(sys.argv) > 3 else ''
            ftypes  = parse_filetypes(sys.argv[4] if len(sys.argv) > 4 else '')
            defext  = ''
            if initfn and '.' in initfn:
                defext = '.' + initfn.rsplit('.', 1)[-1]
            result = filedialog.asksaveasfilename(
                parent=root, title=title,
                initialfile=initfn, defaultextension=defext,
                filetypes=ftypes
            )

        elif mode == 'folder':
            title = sys.argv[2] if len(sys.argv) > 2 else 'Choose folder'
            result = filedialog.askdirectory(parent=root, title=title)

    except Exception as e:
        sys.stderr.write(f"Dialog error: {e}\n")
        result = ''
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    # Write the chosen path to stdout (or empty string if cancelled)
    sys.stdout.write(result or '')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
