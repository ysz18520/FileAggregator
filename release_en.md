## FileAggregator v6.9.0

A 100% offline desktop file aggregator for Windows. Drop a folder in, and your videos, images, and documents are automatically sorted — without ever copying, moving, or modifying your originals.

### What's New in v6.9.0

- **Clean Empty Files** — New green toolbar button scans all indexed folders for 0-byte files, shows a preview list (first 10) in a green confirmation toast, and deletes them in one go. Automatically syncs scan_cache and favorite references.
- **Config Encoding Resilience** — `load_config()` now tries 5 encodings (utf-8 → utf-8-sig → gbk → gb2312 → latin-1), fixing `UnicodeDecodeError` when Windows Notepad saves `config.json` as ANSI.

### What's New in v6.8.0

- **Single-Instance Enforcement** — Clicking the exe again activates the existing window instead of spawning a new one. Powered by a Windows named mutex.
- **Project Search Fix** — Global keyword search now includes project folders. The project view correctly renders filtered results in search mode.
- **Project Batch Action Fixes** — Batch favorite operations now auto-exit batch mode. Batch delete/rename properly syncs the search results list.

### Core Features

| Feature | Description |
|---------|-------------|
| Drag & Drop | Drop any folder into the window to index it instantly |
| Auto-Scan | Recursively scans and aggregates videos, images, and documents |
| Video Player | Built-in HTML5 player for mp4/webm/mov; others open with your default player |
| Image Viewer | Full-size preview with lazy-loaded thumbnails; arrow keys to navigate across folders |
| Documents | One-click open with your system's default app (Word/Excel/PDF/etc.) |
| Favorites | Independent favorite groups for each category; batch move real files via "Relocate" |
| Batch Actions | Multi-select mode with checkbox; batch remove, delete, favorite, or unfavorite |
| Search | Keyword search across all categories and favorites |
| Sorting | Name / Modified / Created / Size, ascending or descending |
| Pagination | 10 / 20 / 50 / 100 items per page |
| Safe by Design | Read-only path mapping; your original files are never touched |

### System Requirements

- Windows 10 (1809+) or Windows 11
- WebView2 Runtime (auto-installed on first launch if missing)

### Download

Extract the entire `dist` folder and run `文件收纳箱v6.9.exe`. No Python or additional dependencies required.
