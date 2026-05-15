## FileAggregator v6.6.0

A 100% offline desktop file aggregator for Windows. Drop a folder in, and your videos, images, and documents are automatically sorted — without ever copying, moving, or modifying your originals.

### What's New in v6.6.0

- **Video Hover Preview Toggle** — Turn preview on/off from the sort bar. Off by default to eliminate memory pressure and UI lag.
- **Preview Performance Boost** — Reduced frame count from 32 to 10 and resolution from 240px to 160px. ~80% smaller payload, 3x faster extraction.
- **Sort by File Size** — New "Size" sort option for the video gallery (ascending/descending), covering main view, search results, and favorites.
- **Memory Release Button** — One-click lightning bolt to flush media cache and preview frame cache, instantly freeing RAM.
- **Per-Page Refresh** — Local refresh buttons on every view (video/image/document/project/favorites) to fix UI state drift without full rescan.
- **Custom Icon Support** — Place `icon.png` and `ui_icon.png` in the project root; build script auto-generates multi-size `icon.ico` for the exe and window branding.

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

Extract the entire `dist` folder and run `文件收纳箱v6.6.exe`. No Python or additional dependencies required.
