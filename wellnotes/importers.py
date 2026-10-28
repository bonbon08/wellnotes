"""Bulk imports: Obsidian vaults, Markdown folders (Logseq, Joplin, …) and Notion ZIP exports."""

import os
import shutil
import tempfile
import zipfile

from . import fileformat
from .markdown import Resolver, find_vault_root

MD_EXTS = (".md", ".markdown", ".mdown", ".mkd")
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "logseq", ".logseq", "_resources"}


def _unique(folder, name, ext=""):
    from .library import unique_path
    return unique_path(folder, name, ext)


def collect(src_dir, include_pdfs=True):
    """List importable files (relative paths) in a folder tree."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for f in sorted(filenames):
            low = f.lower()
            if f.startswith("."):
                continue
            if low.endswith(MD_EXTS) or (include_pdfs and low.endswith(".pdf")):
                out.append(os.path.relpath(os.path.join(dirpath, f), src_dir))
    return out


def _embedded_pdfs(src_dir, md_files):
    """PDFs that some note embeds are imported as part of that note, not separately."""
    import re
    names = set()
    for rel in md_files:
        try:
            with open(os.path.join(src_dir, rel), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r"!\[\[([^\]|#]+\.pdf)", text, re.I):
            names.add(os.path.basename(m.group(1)).lower())
    return names


def import_folder(src_dir, dest_parent, name=None, progress=None, cancelled=None):
    """Import a Markdown folder / Obsidian vault into a new library folder.

    Returns (destination folder, number of notebooks, list of errors).
    ``progress(done, total, current)`` is called from the calling thread.
    """
    vault = find_vault_root(os.path.join(src_dir, "x")) or src_dir
    files = collect(src_dir)
    md_files = [f for f in files if f.lower().endswith(MD_EXTS)]
    skip_pdfs = _embedded_pdfs(src_dir, md_files)
    files = [f for f in files if f.lower().endswith(MD_EXTS) or os.path.basename(f).lower() not in skip_pdfs]
    dest = _unique(dest_parent, fileformat.title_from_path(name or src_dir))
    os.makedirs(dest)
    resolver = Resolver(None, vault)
    errors = []
    done = 0
    for rel in files:
        if cancelled and cancelled():
            break
        src = os.path.join(src_dir, rel)
        if progress:
            progress(done, len(files), rel)
        sub = os.path.dirname(rel)
        out_dir = os.path.join(dest, *[fileformat.title_from_path(p) for p in sub.split(os.sep)]) if sub else dest
        os.makedirs(out_dir, exist_ok=True)
        title = fileformat.title_from_path(src)
        try:
            doc = fileformat.Document(title)
            if src.lower().endswith(".pdf"):
                with open(src, "rb") as f:
                    doc.pages = fileformat.pdf_pages(doc, f.read())
            else:
                with open(src, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                resolver.base_dir = os.path.dirname(src)
                doc.pages = fileformat.markdown_pages(doc, text, os.path.dirname(src), resolver=resolver)
            if doc.pages:
                fileformat.save(doc, _unique(out_dir, title, fileformat.EXT))
                done += 1
        except Exception as e:  # keep importing the rest of the vault
            errors.append("%s: %s" % (rel, e))
    if progress:
        progress(len(files), len(files), "")
    return dest, done, errors


def import_zip(path, dest_parent, progress=None, cancelled=None):
    """Import a ZIP of Markdown files (e.g. a Notion "Markdown & CSV" export)."""
    tmp = tempfile.mkdtemp(prefix="wellnotes-import-")
    try:
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        # Notion wraps large exports in nested "Part-N.zip" archives.
        for dirpath, _, filenames in os.walk(tmp):
            for f in filenames:
                if f.lower().endswith(".zip"):
                    inner = os.path.join(dirpath, f)
                    try:
                        with zipfile.ZipFile(inner) as z:
                            z.extractall(dirpath)
                        os.unlink(inner)
                    except zipfile.BadZipFile:
                        pass
        root = tmp
        entries = [e for e in os.listdir(tmp) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
            root = os.path.join(tmp, entries[0])
        return import_folder(root, dest_parent, name=os.path.splitext(os.path.basename(path))[0],
                             progress=progress, cancelled=cancelled)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
