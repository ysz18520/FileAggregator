"""
========================================================================
  文件收纳箱  -  FileAggregator
========================================================================
  纯本地离线运行 · 仅做路径映射 · 绝不复制/移动/修改任何原始文件

  技术栈:
    - 后端: Python 3 + pywebview
    - 前端: HTML + CSS + Vue (PetiteVue 风格运行时, 内嵌于 ui.html)
    - 数据: 本地 JSON 文件 (用户主目录下的 .file_aggregator/config.json)

  核心原则:
    1. 只保存"文件夹原始绝对路径"到 JSON, 全程引用映射, 不动原文件
    2. 软件内"移除"目录只删除路径记录, 绝对不删本地任何真实文件
    3. 全程纯离线本地运行, 无任何联网请求
========================================================================
"""

import os
import sys
import json
import base64
import shutil
import mimetypes
import subprocess
import threading
from typing import List, Dict, Any, Optional

import webview  # pywebview 桌面窗口框架

# --------------------------------------------------------
# OpenCV 可选导入：用于视频封面截取与预览帧提取
# 若环境中未安装 opencv-python，则视频相关功能优雅降级为图标展示
# --------------------------------------------------------
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ============================================================
# 一、全局配置区
# ============================================================

# 软件信息
APP_NAME = "FileAggregator"
APP_TITLE = "文件收纳箱"
APP_VERSION = "6.7.0"

# 数据存储位置: 用户主目录下的隐藏文件夹, 永不和软件本体混在一起
# 这样即使用户把 exe 换个位置, 数据也不会丢
USER_HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(USER_HOME, ".file_aggregator")
DATA_FILE = os.path.join(DATA_DIR, "config.json")

# ============================================================
# 二、文件分类规则 (扩展名 → 类别)
# ============================================================

# 视频文件扩展名集合
VIDEO_EXTS = {
    '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.mpg', '.mpeg', '.3gp', '.rmvb', '.f4v', '.vob'
}

# 图片文件扩展名集合
IMAGE_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg',
    '.ico', '.tiff', '.tif', '.heic', '.jfif'
}

# 办公文档扩展名集合
DOC_EXTS = {
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.pdf',
    '.txt', '.md', '.csv', '.rtf', '.odt', '.ods', '.odp',
    '.wps', '.et', '.dps'
}

# 内置播放器可直接播放的视频格式 (浏览器 WebView 原生支持)
# 其它格式 (mkv, rmvb 等) 双击会调用系统默认播放器
# .mov 实际多为 H.264+AAC 封装, 现代 Chromium 内核可直接播放
WEB_PLAYABLE_VIDEO = {'.mp4', '.webm', '.ogv', '.ogg', '.m4v', '.mov'}


# ============================================================
# 三、工具函数区
# ============================================================

def resource_path(relative_path: str) -> str:
    """
    获取资源文件的绝对路径
    兼容两种场景:
      1. 开发期直接 `python main.py`  →  当前脚本同级目录
      2. PyInstaller 单文件 exe 运行  →  临时解压目录 `sys._MEIPASS`
    """
    try:
        # PyInstaller 打包后, 资源会被释放到这个临时目录
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    except AttributeError:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)


# 缓存目录
CACHE_DIR = os.path.join(DATA_DIR, "cache")
CACHE_MAX_BYTES = 500 * 1024 * 1024  # 500MB 上限

def ensure_data_dir() -> None:
    """确保数据目录存在 (不存在则创建)"""
    os.makedirs(DATA_DIR, exist_ok=True)


def _cache_key(file_path: str) -> str:
    """根据文件路径生成缓存键 (SHA256 前 16 位)"""
    import hashlib
    return hashlib.sha256(file_path.encode('utf-8')).hexdigest()[:16]


def _cache_path(subdir: str, file_path: str, ext: str = '.jpg') -> str:
    """获取缓存文件的完整路径"""
    d = os.path.join(CACHE_DIR, subdir)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _cache_key(file_path) + ext)


def _read_cache(subdir: str, file_path: str) -> Optional[bytes]:
    """读取缓存文件，返回二进制内容；无缓存或过期返回 None"""
    cp = _cache_path(subdir, file_path)
    if not os.path.exists(cp):
        return None
    # 简单校验：缓存文件的修改时间必须 >= 原文件修改时间
    try:
        if os.path.getmtime(cp) < os.path.getmtime(file_path):
            return None
    except Exception:
        pass
    try:
        with open(cp, 'rb') as f:
            return f.read()
    except Exception:
        return None


def _write_cache(subdir: str, file_path: str, data: bytes, ext: str = '.jpg') -> None:
    """写入缓存文件，并在总大小超限时执行 LRU 淘汰"""
    cp = _cache_path(subdir, file_path, ext)
    try:
        with open(cp, 'wb') as f:
            f.write(data)
    except Exception:
        return
    # LRU 清理：如果缓存总大小超过上限，删除最旧的文件
    _clean_cache_if_needed()


def _clean_cache_if_needed() -> None:
    """按 LRU 策略清理缓存，保持总大小在上限以内"""
    try:
        files = []
        total = 0
        for root, _, names in os.walk(CACHE_DIR):
            for n in names:
                p = os.path.join(root, n)
                s = os.path.getsize(p)
                m = os.path.getmtime(p)
                files.append((m, s, p))
                total += s
        if total <= CACHE_MAX_BYTES:
            return
        # 按修改时间升序（最旧的在前面）
        files.sort(key=lambda x: x[0])
        for _, s, p in files:
            if total <= CACHE_MAX_BYTES:
                break
            try:
                os.remove(p)
                total -= s
            except Exception:
                pass
    except Exception:
        pass


def load_config() -> Dict[str, Any]:
    """
    加载本地 JSON 配置文件
    出现任何异常 (文件损坏/格式错误) 时返回空配置, 不让程序崩溃
    """
    ensure_data_dir()
    if not os.path.exists(DATA_FILE):
        return {"folders": [], "project_folders": [], "favorites": {"video": [], "image": [], "document": [], "project": []}}
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 兼容性校验: 确保结构合法
            if not isinstance(data, dict):
                return {"folders": [], "project_folders": [], "favorites": {"video": [], "image": [], "document": [], "project": []}}
            if "folders" not in data or not isinstance(data["folders"], list):
                data["folders"] = []
            # 兼容旧版配置: 自动初始化 project_folders 字段
            if "project_folders" not in data or not isinstance(data["project_folders"], list):
                data["project_folders"] = []
            # 兼容旧版配置: 自动初始化 favorites 字段
            if "favorites" not in data or not isinstance(data["favorites"], dict):
                data["favorites"] = {"video": [], "image": [], "document": [], "project": []}
            else:
                for cat in ["video", "image", "document", "project"]:
                    if cat not in data["favorites"] or not isinstance(data["favorites"][cat], list):
                        data["favorites"][cat] = []
            # 兼容旧版配置: 自动初始化 settings 字段
            if "settings" not in data or not isinstance(data["settings"], dict):
                data["settings"] = {"sort_by": "name", "sort_order": "asc", "page_size": 20, "video_preview_enabled": False}
            else:
                defaults = {"sort_by": "name", "sort_order": "asc", "page_size": 20, "video_preview_enabled": False}
                for k, v in defaults.items():
                    if k not in data["settings"]:
                        data["settings"][k] = v
            return data
    except (json.JSONDecodeError, OSError):
        return {"folders": [], "project_folders": [], "favorites": {"video": [], "image": [], "document": [], "project": []}}


def save_config(config: Dict[str, Any]) -> None:
    """
    保存配置到本地 JSON 文件
    使用 utf-8 + indent=2, 让 JSON 文件人类可读、方便用户手动备份
    """
    ensure_data_dir()
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def normalize_path(p: str) -> str:
    """
    路径标准化
    把 / 反斜杠等不一致的写法统一, 避免重复添加同一个文件夹
    """
    if not p:
        return p
    return os.path.normpath(os.path.abspath(p))


import time as _time
import random as _random


def _generate_fav_id() -> str:
    """生成唯一收藏夹 ID"""
    return f"fv_{int(_time.time() * 1000)}_{_random.randint(1000, 9999)}"


def get_category(filename: str) -> Optional[str]:
    """
    根据文件名后缀返回所属分类
    返回值: 'video' / 'image' / 'document' / None
    None 表示不属于我们关心的三大类, 扫描时直接跳过
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in DOC_EXTS:
        return 'document'
    return None


def scan_folder(folder_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    递归扫描一个文件夹, 返回按类别分组的文件信息列表

    返回结构:
      {
        'video':    [ {name, path, size, modified, folder, ext}, ... ],
        'image':    [ ... ],
        'document': [ ... ],
      }

    注意:
      - 全程只读, 用 os.walk 遍历, 不修改任何文件
      - 拿不到 stat 信息或没权限的文件会被静默跳过
    """
    result: Dict[str, List[Dict[str, Any]]] = {
        'video': [], 'image': [], 'document': []
    }

    if not folder_path or not os.path.isdir(folder_path):
        return result

    try:
        for root, _dirs, files in os.walk(folder_path):
            for name in files:
                category = get_category(name)
                if category is None:
                    continue  # 跳过非三大类文件

                full_path = os.path.join(root, name)
                try:
                    stat = os.stat(full_path)
                    ext = os.path.splitext(name)[1].lower()
                    result[category].append({
                        'name': name,
                        'path': full_path,
                        'size': stat.st_size,         # 字节数
                        'modified': stat.st_mtime,    # 最后修改时间戳
                        'created': getattr(stat, 'st_birthtime', stat.st_ctime),  # 创建时间戳
                        'folder': folder_path,        # 所属收录目录
                        'ext': ext,                   # 扩展名 (小写)
                        # 视频是否能用内置 HTML5 video 标签播放
                        'web_playable': ext in WEB_PLAYABLE_VIDEO
                                        if category == 'video' else False,
                    })
                except OSError:
                    # 文件无法访问 (权限/已删除等) 直接跳过
                    continue
    except (PermissionError, OSError):
        # 整个目录无权限时也不让程序崩溃
        pass

    return result


# ============================================================
# 四、暴露给前端 JavaScript 调用的 API 类
# ============================================================
#
# pywebview 会自动把这个类的所有公有方法暴露到前端,
# 前端通过 window.pywebview.api.方法名(参数) 调用,
# 返回值默认会被 JSON 序列化后传回前端 (Promise).
#
# ============================================================

class Api:
    """前端与后端通讯的桥梁类"""

    def __init__(self):
        # 窗口实例, 后续 _set_window 注入
        # 用于打开系统文件夹选择对话框等需要窗口上下文的操作
        #
        # 【重要】属性名 / 方法名都以 _ 开头, pywebview 会自动跳过、不暴露给 JS
        # 否则 webview.Window 不是 JSON-serializable, 部分 pywebview 版本会
        # 因为内省失败而把整套 API 方法都不绑出去, 表现为前端 api 对象空壳.
        self._window: Optional[webview.Window] = None

    def _set_window(self, window: webview.Window) -> None:
        """注入主窗口引用 (在 main 函数中调用) —— 内部方法, 不暴露到 JS"""
        self._window = window

    # --------------------------------------------------------
    # 4.1 目录管理 (增/删/查)
    # --------------------------------------------------------

    def get_folders(self) -> List[str]:
        """查询所有已收录的文件夹路径列表"""
        return load_config().get('folders', [])

    def add_folder(self, folder_path: str) -> Dict[str, Any]:
        """
        添加一个新文件夹到收录列表
        参数: folder_path  —  本地绝对路径
        返回: {ok: bool, msg: str}

        【纪律】这里只把路径字符串写入 JSON, 绝不动用户的原始文件
        """
        if not folder_path:
            return {'ok': False, 'msg': '路径为空'}

        folder_path = normalize_path(folder_path)

        # 必须是真实存在的文件夹
        if not os.path.isdir(folder_path):
            return {'ok': False, 'msg': '路径不存在或不是文件夹'}

        config = load_config()
        folders = config.get('folders', [])

        # 去重: 同一路径不重复添加
        if folder_path in folders:
            return {'ok': False, 'msg': '该文件夹已经添加过了'}

        folders.append(folder_path)
        config['folders'] = folders
        save_config(config)
        return {'ok': True, 'msg': f'添加成功: {folder_path}'}

    def remove_folder(self, folder_path: str) -> Dict[str, Any]:
        """
        移除一个收录的文件夹
        【纪律】只删 JSON 里的路径记录, 绝对不删任何本地真实文件
        """
        folder_path = normalize_path(folder_path)
        config = load_config()
        folders = config.get('folders', [])

        if folder_path not in folders:
            return {'ok': False, 'msg': '收录列表中未找到该目录'}

        folders.remove(folder_path)
        config['folders'] = folders
        save_config(config)
        return {'ok': True, 'msg': '已从软件中移除 (本地原文件未做任何改动)'}

    # --------------------------------------------------------
    # 4.1.1 项目文件夹管理 (增/删/查)
    # --------------------------------------------------------

    def get_project_folders(self) -> List[str]:
        """查询所有已收录的项目文件夹路径列表"""
        return load_config().get('project_folders', [])

    def add_project_folder(self, folder_path: str) -> Dict[str, Any]:
        """添加一个新项目文件夹到收录列表 (不遍历内部文件)"""
        if not folder_path:
            return {'ok': False, 'msg': '路径为空'}
        folder_path = normalize_path(folder_path)
        if not os.path.isdir(folder_path):
            return {'ok': False, 'msg': '路径不存在或不是文件夹'}
        config = load_config()
        projects = config.get('project_folders', [])
        if folder_path in projects:
            return {'ok': False, 'msg': '该项目文件夹已经添加过了'}
        projects.append(folder_path)
        config['project_folders'] = projects
        save_config(config)
        return {'ok': True, 'msg': f'添加成功: {folder_path}'}

    def remove_project_folder(self, folder_path: str) -> Dict[str, Any]:
        """移除一个收录的项目文件夹"""
        folder_path = normalize_path(folder_path)
        config = load_config()
        projects = config.get('project_folders', [])
        if folder_path not in projects:
            return {'ok': False, 'msg': '收录列表中未找到该项目目录'}
        projects.remove(folder_path)
        config['project_folders'] = projects
        save_config(config)
        return {'ok': True, 'msg': '已从软件中移除 (本地原文件未做任何改动)'}

    def delete_project_folder_real(self, folder_path: str) -> Dict[str, Any]:
        """【高危操作】永久删除本地真实项目文件夹"""
        folder_path = normalize_path(folder_path)
        if not os.path.exists(folder_path):
            return {'ok': False, 'msg': '文件夹不存在'}
        if not os.path.isdir(folder_path):
            return {'ok': False, 'msg': '目标不是文件夹'}
        try:
            shutil.rmtree(folder_path)
        except Exception as e:
            return {'ok': False, 'msg': f'删除失败: {e}'}
        # 清理软件内映射和收藏
        self.remove_project_folder(folder_path)
        # 从 project 分类的收藏夹中清理
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get('project', [])
        for g in groups:
            items = g.get('items', [])
            g['items'] = [p for p in items if normalize_path(p) != folder_path]
        favorites['project'] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': '已永久删除本地文件夹'}

    def batch_remove_project_folders(self, folder_paths: List[str]) -> Dict[str, Any]:
        """批量从软件映射中移除项目文件夹"""
        if not folder_paths:
            return {'ok': True, 'msg': '没有需要处理的文件夹'}
        success = 0
        failed = 0
        for p in folder_paths:
            result = self.remove_project_folder(p)
            if result.get('ok'):
                success += 1
            else:
                failed += 1
        return {'ok': failed == 0, 'msg': f'移除完成: 成功 {success} 个, 失败 {failed} 个'}

    def batch_delete_project_real(self, folder_paths: List[str]) -> Dict[str, Any]:
        """批量永久删除本地项目文件夹"""
        if not folder_paths:
            return {'ok': True, 'msg': '没有需要处理的文件夹'}
        success = 0
        failed = 0
        for p in folder_paths:
            result = self.delete_project_folder_real(p)
            if result.get('ok'):
                success += 1
            else:
                failed += 1
        return {'ok': failed == 0, 'msg': f'删除完成: 成功 {success} 个, 失败 {failed} 个'}

    def get_project_items(self) -> List[Dict[str, Any]]:
        """返回所有项目文件夹的元数据列表 (不遍历内部文件)"""
        config = load_config()
        projects = config.get('project_folders', [])
        favorites = config.get('favorites', {})
        # 构建 project 分类的已收藏路径集合
        fav_set = set()
        for g in favorites.get('project', []):
            for p in g.get('items', []):
                fav_set.add(normalize_path(p))
        result = []
        for path in projects:
            try:
                stat = os.stat(path)
                name = os.path.basename(path)
                result.append({
                    'name': name,
                    'path': path,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'created': getattr(stat, 'st_birthtime', stat.st_ctime),
                    'is_favorited': normalize_path(path) in fav_set,
                })
            except (OSError, PermissionError):
                continue
        # 按修改时间倒序
        result.sort(key=lambda x: x['modified'], reverse=True)
        return result

    # --------------------------------------------------------
    # 4.2 文件扫描 (全部刷新 / 单目录刷新)
    # --------------------------------------------------------

    def scan_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        扫描所有收录目录, 返回聚合后的三类文件列表
        所有结果按"最后修改时间"倒序排列
        同时标记每个文件是否已被收藏 (is_favorited)
        扫描完成后自动保存缓存到 config.json, 下次启动可直接读取避免重复遍历
        """
        config = load_config()
        folders: List[str] = config.get('folders', [])
        favorites = config.get('favorites', {})

        # 构建每个分类的已收藏路径集合, 用于快速查询
        fav_sets: Dict[str, set] = {}
        for cat in ['video', 'image', 'document']:
            fav_sets[cat] = set()
            for g in favorites.get(cat, []):
                for p in g.get('items', []):
                    fav_sets[cat].add(normalize_path(p))

        merged: Dict[str, List[Dict[str, Any]]] = {
            'video': [], 'image': [], 'document': []
        }
        scan_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

        for folder in folders:
            part = scan_folder(folder)
            # 缓存原始扫描结果(不含 is_favorited, 该字段是动态计算的)
            scan_cache[folder] = {
                cat: [{k: v for k, v in item.items() if k != 'is_favorited'} for item in part[cat]]
                for cat in part
            }
            for cat in ['video', 'image', 'document']:
                for item in part[cat]:
                    item['is_favorited'] = normalize_path(item['path']) in fav_sets[cat]
                merged[cat].extend(part[cat])

        # 按修改时间倒序, 最近修改的排前面
        for key in merged:
            merged[key].sort(key=lambda x: x['modified'], reverse=True)

        # 保存扫描缓存, 下次启动直接读取避免重复遍历
        config['scan_cache'] = scan_cache
        save_config(config)

        return merged

    def scan_one(self, folder_path: str) -> Dict[str, List[Dict[str, Any]]]:
        """扫描单个目录, 用于"刷新此目录"按钮; 同时更新该目录的扫描缓存"""
        folder_path = normalize_path(folder_path)
        config = load_config()
        favorites = config.get('favorites', {})

        fav_sets: Dict[str, set] = {}
        for cat in ['video', 'image', 'document']:
            fav_sets[cat] = set()
            for g in favorites.get(cat, []):
                for p in g.get('items', []):
                    fav_sets[cat].add(normalize_path(p))

        result = scan_folder(folder_path)
        for cat in ['video', 'image', 'document']:
            for item in result[cat]:
                item['is_favorited'] = normalize_path(item['path']) in fav_sets[cat]
            result[cat].sort(key=lambda x: x['modified'], reverse=True)

        # 更新该目录的扫描缓存
        scan_cache = config.get('scan_cache', {})
        scan_cache[folder_path] = {
            cat: [{k: v for k, v in item.items() if k != 'is_favorited'} for item in result[cat]]
            for cat in result
        }
        config['scan_cache'] = scan_cache
        save_config(config)

        return result

    def get_cached_files(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        启动时快速加载缓存的文件列表, 避免重复扫描磁盘。
        如果缓存为空或收录目录有变化(新增/删除目录), 返回空列表让前端调用 scan_all。
        全程包裹 try-except, 确保任何数据异常都不会导致启动崩溃或空白。
        """
        try:
            config = load_config()
            scan_cache = config.get('scan_cache', {})
            folders = config.get('folders', [])
            favorites = config.get('favorites', {})

            if not scan_cache:
                return {'video': [], 'image': [], 'document': []}

            # 如果收录目录有变化(新增/删除), 缓存失效, 让前端重新扫描
            cached_folders = set(scan_cache.keys())
            current_folders = set(folders)
            if cached_folders != current_folders:
                return {'video': [], 'image': [], 'document': []}

            # 重建收藏集合(收藏状态是动态的, 不能缓存)
            fav_sets: Dict[str, set] = {}
            for cat in ['video', 'image', 'document']:
                fav_sets[cat] = set()
                for g in favorites.get(cat, []):
                    for p in g.get('items', []):
                        fav_sets[cat].add(normalize_path(p))

            merged: Dict[str, List[Dict[str, Any]]] = {
                'video': [], 'image': [], 'document': []
            }
            for folder in folders:
                part = scan_cache.get(folder)
                if not part or not isinstance(part, dict):
                    continue
                for cat in ['video', 'image', 'document']:
                    cat_list = part.get(cat)
                    if not isinstance(cat_list, list):
                        continue
                    for item in cat_list:
                        if not isinstance(item, dict):
                            continue
                        item_copy = dict(item)
                        item_copy['is_favorited'] = normalize_path(item_copy.get('path', '')) in fav_sets[cat]
                        merged[cat].append(item_copy)

            for key in merged:
                merged[key].sort(key=lambda x: x.get('modified', 0), reverse=True)

            return merged
        except Exception:
            # 任何异常都返回空数据, 让前端走正常扫描路径兜底
            return {'video': [], 'image': [], 'document': []}

    # --------------------------------------------------------
    # 4.3 文件操作 (调用系统默认程序打开 / 打开所在位置)
    # --------------------------------------------------------

    def open_file(self, file_path: str) -> Dict[str, Any]:
        """
        用系统默认软件打开文件 (文档专用 - 调用 Word/Excel/PDF 阅读器等)
        Windows: os.startfile  /  macOS: open  /  Linux: xdg-open
        """
        if not os.path.exists(file_path):
            return {'ok': False, 'msg': '文件不存在 (可能已被移动或删除)'}
        try:
            if sys.platform == 'win32':
                os.startfile(file_path)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', file_path])
            else:
                subprocess.Popen(['xdg-open', file_path])
            return {'ok': True, 'msg': '已用默认程序打开'}
        except Exception as e:
            return {'ok': False, 'msg': f'打开失败: {e}'}

    def show_in_folder(self, file_path: str) -> Dict[str, Any]:
        """
        打开"文件所在位置"
        Windows 下使用 explorer /select 参数, 可直接定位并高亮该文件
        """
        if not os.path.exists(file_path):
            return {'ok': False, 'msg': '文件不存在'}
        try:
            if sys.platform == 'win32':
                # 注意: /select, 后面要紧跟一个英文逗号
                subprocess.Popen(
                    ['explorer', '/select,', os.path.normpath(file_path)]
                )
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', file_path])
            else:
                # Linux 没有统一选中文件的方式, 退而求其次打开父目录
                subprocess.Popen(['xdg-open', os.path.dirname(file_path)])
            return {'ok': True, 'msg': '已在文件管理器中定位'}
        except Exception as e:
            return {'ok': False, 'msg': f'打开失败: {e}'}

    def open_folder(self, folder_path: str) -> Dict[str, Any]:
        """打开指定的文件夹 (而不是选中里面的某个文件)"""
        if not os.path.isdir(folder_path):
            return {'ok': False, 'msg': '文件夹不存在'}
        try:
            if sys.platform == 'win32':
                os.startfile(folder_path)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder_path])
            else:
                subprocess.Popen(['xdg-open', folder_path])
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': f'打开失败: {e}'}

    # --------------------------------------------------------
    # 4.4 媒体读取 (图片预览 / 视频流) - 通过 base64 数据 URL
    # --------------------------------------------------------

    def read_image_base64(self, file_path: str,
                          max_bytes: int = 30 * 1024 * 1024) -> Optional[str]:
        """
        读取图片文件并转为 Base64 Data URL
        让前端 <img> 标签可直接渲染 (避开 file:// 协议在某些 webview 上的限制)
        max_bytes 限制最大 30MB, 防止超大图把内存撑爆
        """
        try:
            if not os.path.exists(file_path):
                return None
            size = os.path.getsize(file_path)
            if size > max_bytes:
                return None
            ext = os.path.splitext(file_path)[1].lower()
            mime = mimetypes.types_map.get(ext, 'image/jpeg')
            with open(file_path, 'rb') as f:
                data = f.read()
            b64 = base64.b64encode(data).decode('ascii')
            return f"data:{mime};base64,{b64}"
        except Exception:
            return None

    def get_image_thumb(self, file_path: str,
                        max_size: int = 400,
                        quality: int = 80) -> Optional[str]:
        """
        生成图片缩略图并返回 base64 JPEG Data URL
        优先读磁盘缓存，miss 后用 Pillow 缩放后写入缓存
        若 Pillow 不可用则回退到 read_image_base64（原图）
        max_size: 最大边长（宽高取大者缩放到此值）
        """
        # 优先读缓存
        cached = _read_cache('thumbs', file_path)
        if cached is not None:
            return f"data:image/jpeg;base64,{base64.b64encode(cached).decode('ascii')}"

        if not HAS_PIL:
            return self.read_image_base64(file_path)

        try:
            if not os.path.exists(file_path):
                return None

            # 打开原图
            img = Image.open(file_path)
            # 处理旋转信息（部分相机照片带 EXIF 方向）
            img = img.convert('RGB')

            # 等比缩放到最大边 max_size
            w, h = img.size
            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # 编码为 JPEG
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True)
            data = buf.getvalue()

            _write_cache('thumbs', file_path, data)
            return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        except Exception:
            # 任何异常回退到原图
            return self.read_image_base64(file_path)

    def read_video_base64(self, file_path: str,
                          max_bytes: int = 200 * 1024 * 1024) -> Optional[str]:
        """
        读取视频并转 Base64 Data URL, 让内置 <video> 标签播放
        超过 max_bytes (默认 200MB) 的视频返回 None, 让前端走"用系统播放器打开"路径
        """
        try:
            if not os.path.exists(file_path):
                return None
            size = os.path.getsize(file_path)
            if size > max_bytes:
                return None
            ext = os.path.splitext(file_path)[1].lower()
            # .mov 文件实际多为 H.264+AAC 封装, 浏览器以 video/mp4 播放兼容性更好
            if ext == '.mov':
                mime = 'video/mp4'
            else:
                mime = mimetypes.types_map.get(ext, 'video/mp4')
            with open(file_path, 'rb') as f:
                data = f.read()
            b64 = base64.b64encode(data).decode('ascii')
            return f"data:{mime};base64,{b64}"
        except Exception:
            return None

    # --------------------------------------------------------
    # 4.5 视频封面与悬浮预览帧提取 (OpenCV)
    # --------------------------------------------------------
    # 全程只读原视频文件，不做任何转码/复制/修改/导出操作

    def get_video_cover(self, file_path: str) -> Optional[str]:
        """
        截取视频中间帧作为静态封面，返回 base64 JPEG Data URL
        优先读取磁盘缓存，miss 后提取并写入缓存
        若未安装 OpenCV 或读取失败则返回 None，前端回退为默认图标
        """
        # 先尝试读缓存
        cached = _read_cache('covers', file_path)
        if cached is not None:
            return f"data:image/jpeg;base64,{base64.b64encode(cached).decode('ascii')}"

        if not HAS_CV2:
            return None
        try:
            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                return None

            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total <= 0:
                cap.release()
                return None

            # 定位到中间帧，作为代表性封面
            target = total // 2
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = cap.read()
            cap.release()

            if not ok or frame is None:
                return None

            # 限制最大宽度，生成轻量缩略图
            max_w = 320
            h, w = frame.shape[:2]
            if w > max_w:
                ratio = max_w / w
                frame = cv2.resize(frame, (max_w, int(h * ratio)), interpolation=cv2.INTER_AREA)

            # 编码为 JPEG
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return None

            data = buf.tobytes()
            _write_cache('covers', file_path, data)
            b64 = base64.b64encode(data).decode('ascii')
            return f"data:image/jpeg;base64,{b64}"
        except Exception:
            return None

    def get_video_preview_frames(self, file_path: str,
                                 frame_count: int = 10,
                                 max_width: int = 160) -> Optional[List[str]]:
        """
        提取视频预览帧序列，用于鼠标悬浮时连贯循环播放
        返回: base64 JPEG Data URL 列表
        全程只读原视频，不做任何转码/复制/修改/导出
        """
        if not HAS_CV2:
            return None
        try:
            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                return None

            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total <= 0:
                cap.release()
                return None

            # 从视频全程均匀抽取 frame_count 帧，覆盖整段内容
            step = total / frame_count
            indices = [min(int(i * step), total - 1) for i in range(frame_count)]

            frames_b64: List[str] = []
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue

                # 限制宽度，控制单帧体积
                h, w = frame.shape[:2]
                if w > max_width:
                    ratio = max_width / w
                    frame = cv2.resize(frame, (max_width, int(h * ratio)), interpolation=cv2.INTER_AREA)

                ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                if ok:
                    b64 = base64.b64encode(buf.tobytes()).decode('ascii')
                    frames_b64.append(f"data:image/jpeg;base64,{b64}")

            cap.release()
            return frames_b64 if frames_b64 else None
        except Exception:
            return None

    # --------------------------------------------------------
    # 4.6 永久删除本地真实文件夹（高危操作）
    # --------------------------------------------------------
    # 仅应在用户明确二次确认后调用，和普通移除做好区分

    def delete_folder_permanently(self, folder_path: str) -> Dict[str, Any]:
        """
        【高危操作】永久删除本地真实文件夹及其中全部内容
        调用 shutil.rmtree 执行真实文件系统删除，不可恢复
        同时清除软件内的路径记录
        """
        import shutil
        if not folder_path:
            return {'ok': False, 'msg': '路径为空'}

        folder_path = normalize_path(folder_path)

        if not os.path.exists(folder_path):
            return {'ok': False, 'msg': '路径不存在'}
        if not os.path.isdir(folder_path):
            return {'ok': False, 'msg': '目标不是文件夹'}

        try:
            shutil.rmtree(folder_path)
            # 同步从软件记录中移除，避免残留无效路径
            config = load_config()
            folders = config.get('folders', [])
            if folder_path in folders:
                folders.remove(folder_path)
                config['folders'] = folders
                save_config(config)
            return {'ok': True, 'msg': '已永久删除本地文件夹'}
        except Exception as e:
            return {'ok': False, 'msg': f'删除失败: {e}'}

    # --------------------------------------------------------
    # 4.8 收藏夹管理 (创建/重命名/删除/添加/移除/查询)
    # --------------------------------------------------------
    # 三个分类 (video/image/document) 完全独立, 互不交叉
    # 只保存文件路径字符串, 不动原文件

    def get_favorite_groups(self, category: str) -> List[Dict[str, Any]]:
        """获取某分类下的所有收藏夹列表"""
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        # 返回深拷贝, 避免前端意外修改
        return [
            {'id': g.get('id'), 'name': g.get('name'), 'count': len(g.get('items', []))}
            for g in groups
        ]

    def create_favorite_group(self, category: str, name: str) -> Dict[str, Any]:
        """创建新的收藏夹"""
        if not name or not name.strip():
            return {'ok': False, 'msg': '收藏夹名称不能为空'}
        name = name.strip()
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        # 同分类下名称去重
        for g in groups:
            if g.get('name') == name:
                return {'ok': False, 'msg': '该收藏夹名称已存在'}
        new_group = {'id': _generate_fav_id(), 'name': name, 'items': []}
        groups.append(new_group)
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': f'收藏夹 "{name}" 创建成功', 'group': new_group}

    def rename_favorite_group(self, category: str, group_id: str, new_name: str) -> Dict[str, Any]:
        """重命名收藏夹"""
        if not new_name or not new_name.strip():
            return {'ok': False, 'msg': '收藏夹名称不能为空'}
        new_name = new_name.strip()
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target = None
        for g in groups:
            if g.get('id') == group_id:
                target = g
                break
        if not target:
            return {'ok': False, 'msg': '收藏夹不存在'}
        # 检查名称是否与其他收藏夹重复
        for g in groups:
            if g.get('id') != group_id and g.get('name') == new_name:
                return {'ok': False, 'msg': '该收藏夹名称已存在'}
        target['name'] = new_name
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': '重命名成功'}

    def delete_favorite_group(self, category: str, group_id: str) -> Dict[str, Any]:
        """删除收藏夹 (连同其中的收藏记录一起删除)"""
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        new_groups = [g for g in groups if g.get('id') != group_id]
        if len(new_groups) == len(groups):
            return {'ok': False, 'msg': '收藏夹不存在'}
        favorites[category] = new_groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': '收藏夹已删除'}

    def add_to_favorites(self, category: str, group_id: str, file_path: str) -> Dict[str, Any]:
        """添加文件到收藏夹"""
        if not file_path:
            return {'ok': False, 'msg': '文件路径为空'}
        file_path = normalize_path(file_path)
        if not os.path.exists(file_path):
            return {'ok': False, 'msg': '文件不存在'}
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target = None
        for g in groups:
            if g.get('id') == group_id:
                target = g
                break
        if not target:
            return {'ok': False, 'msg': '收藏夹不存在'}
        items = target.get('items', [])
        # 同收藏夹内去重
        if file_path in items:
            return {'ok': False, 'msg': '该文件已在此收藏夹中'}
        items.append(file_path)
        target['items'] = items
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': '收藏成功'}

    def remove_from_favorites(self, category: str, group_id: str, file_path: str) -> Dict[str, Any]:
        """从收藏夹移除文件"""
        if not file_path:
            return {'ok': False, 'msg': '文件路径为空'}
        file_path = normalize_path(file_path)
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target = None
        for g in groups:
            if g.get('id') == group_id:
                target = g
                break
        if not target:
            return {'ok': False, 'msg': '收藏夹不存在'}
        items = target.get('items', [])
        if file_path not in items:
            return {'ok': False, 'msg': '该文件不在此收藏夹中'}
        items.remove(file_path)
        target['items'] = items
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': '已取消收藏'}

    def get_favorite_items(self, category: str, group_id: str) -> List[Dict[str, Any]]:
        """
        获取收藏夹内的文件详细信息列表
        返回结构与 scan_all 中的文件条目同构, 便于前端复用渲染逻辑
        """
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target = None
        for g in groups:
            if g.get('id') == group_id:
                target = g
                break
        if not target:
            return []

        items: List[Dict[str, Any]] = []
        for path in target.get('items', []):
            if not os.path.exists(path):
                continue
            try:
                stat = os.stat(path)
                name = os.path.basename(path)
                ext = os.path.splitext(name)[1].lower()
                item = {
                    'name': name,
                    'path': path,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'created': getattr(stat, 'st_birthtime', stat.st_ctime),
                    'folder': os.path.dirname(path),
                    'ext': ext,
                }
                if category == 'video':
                    item['web_playable'] = ext in WEB_PLAYABLE_VIDEO
                items.append(item)
            except OSError:
                continue
        # 按修改时间倒序
        items.sort(key=lambda x: x['modified'], reverse=True)
        return items

    def is_favorited(self, category: str, file_path: str) -> Optional[str]:
        """
        检查文件是否已收藏, 返回所在收藏夹 ID; 未收藏返回 null
        """
        if not file_path:
            return None
        file_path = normalize_path(file_path)
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        for g in groups:
            if file_path in g.get('items', []):
                return g.get('id')
        return None

    # --------------------------------------------------------
    # 4.9 设置 (排序 / 分页)
    # --------------------------------------------------------

    def get_version(self) -> str:
        """获取软件版本号"""
        return APP_VERSION

    def get_settings(self) -> Dict[str, Any]:
        """获取用户设置 (排序方式、分页大小等)"""
        config = load_config()
        s = config.get('settings', {"sort_by": "name", "sort_order": "asc", "page_size": 20, "video_preview_enabled": False})
        if 'video_preview_enabled' not in s:
            s['video_preview_enabled'] = False
        return s

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """保存用户设置"""
        config = load_config()
        config['settings'] = settings
        save_config(config)
        return {'ok': True, 'msg': '设置已保存'}

    # --------------------------------------------------------
    # 4.10 系统文件夹选择对话框
    # --------------------------------------------------------

    def pick_folder(self) -> Dict[str, Any]:
        """
        弹出系统的文件夹选择对话框, 仅返回路径不自动添加
        由前端决定添加到遍历目录还是项目目录
        """
        if not self._window:
            return {'ok': False, 'msg': '窗口未就绪'}
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG  # 文件夹选择模式
            )
        except Exception as e:
            return {'ok': False, 'msg': f'对话框打开失败: {e}'}

        if not result:
            return {'ok': False, 'msg': '已取消选择'}

        # result 是一个 tuple 或 list, 取第一个
        folder = result[0] if isinstance(result, (list, tuple)) else result
        return {'ok': True, 'path': folder}

    def pick_target_folder(self) -> Dict[str, Any]:
        """
        【一键搬迁专用】弹出文件夹选择对话框, 仅返回路径, 不自动添加到收录列表
        """
        if not self._window:
            return {'ok': False, 'msg': '窗口未就绪'}
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:
            return {'ok': False, 'msg': f'对话框打开失败: {e}'}
        if not result:
            return {'ok': False, 'msg': '已取消选择'}
        folder = result[0] if isinstance(result, (list, tuple)) else result
        return {'ok': True, 'path': normalize_path(folder)}

    def move_fav_group(self, category: str, group_id: str, target_path: str) -> Dict[str, Any]:
        """
        【一键搬迁 · 高危操作】将收藏夹内所有文件物理搬迁到目标路径下的同名文件夹中
        步骤:
          1. 在 target_path 下创建与收藏夹同名的文件夹
          2. 批量 shutil.move 剪切移动所有文件
          3. 自动处理文件名冲突(加序号)
          4. 更新 config.json 中收藏夹的文件路径为新路径
          5. 返回成功/失败明细
        """
        import shutil
        if not target_path:
            return {'ok': False, 'msg': '目标路径为空'}
        target_path = normalize_path(target_path)
        if not os.path.isdir(target_path):
            return {'ok': False, 'msg': '目标路径不存在或不是文件夹'}

        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target_group = None
        for g in groups:
            if g.get('id') == group_id:
                target_group = g
                break
        if not target_group:
            return {'ok': False, 'msg': '收藏夹不存在'}

        # 在目标路径下创建与收藏夹同名的文件夹, 名称中去除 Windows 非法字符
        group_name = target_group.get('name', '未命名收藏夹')
        safe_name = "".join(c for c in group_name if c not in '\\/:*?"<>|').strip()
        if not safe_name:
            safe_name = '搬迁文件夹'
        dest_dir = os.path.join(target_path, safe_name)
        original_dest_dir = dest_dir
        counter = 1
        while os.path.exists(dest_dir):
            dest_dir = f"{original_dest_dir}_{counter}"
            counter += 1
        os.makedirs(dest_dir, exist_ok=True)

        items = target_group.get('items', [])
        if not items:
            return {'ok': False, 'msg': '收藏夹为空, 无需搬迁'}

        moved = []
        failed = []
        new_items = []

        for src_path in items:
            if not os.path.exists(src_path):
                failed.append({'path': src_path, 'reason': '源文件不存在'})
                continue
            file_name = os.path.basename(src_path)
            dest_file = os.path.join(dest_dir, file_name)
            # 处理目标位置已有同名文件的情况: 使用收藏夹名+序号作为新文件名
            if os.path.exists(dest_file):
                ext = os.path.splitext(file_name)[1]
                c = 1
                while os.path.exists(dest_file):
                    dest_file = os.path.join(dest_dir, f"{safe_name}_{c}{ext}")
                    c += 1
            try:
                shutil.move(src_path, dest_file)
                moved.append({'old': src_path, 'new': dest_file})
                new_items.append(dest_file)
            except Exception as e:
                failed.append({'path': src_path, 'reason': str(e)})

        # 更新收藏夹路径: 成功移动的改为新路径, 失败的保留原路径
        target_group['items'] = new_items + [f['path'] for f in failed]
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)

        return {
            'ok': len(moved) > 0,
            'msg': f'搬迁完成: 成功 {len(moved)} 个, 失败 {len(failed)} 个',
            'moved': moved,
            'failed': failed,
            'dest_dir': dest_dir
        }

    # --------------------------------------------------------
    # 4.11 文件级删除 (映射删除 / 真实删除)
    # --------------------------------------------------------

    def delete_file_map(self, file_path: str, category: str) -> Dict[str, Any]:
        """
        从软件映射中移除单个文件 (不删本地原文件)
        同时从 scan_cache 和 favorites 中清理引用
        """
        if not file_path:
            return {'ok': False, 'msg': '路径为空'}
        file_path = normalize_path(file_path)
        config = load_config()

        # 1. 从 scan_cache 中移除
        scan_cache = config.get('scan_cache', {})
        for _, folder_data in scan_cache.items():
            cat_list = folder_data.get(category, [])
            new_list = [item for item in cat_list if normalize_path(item.get('path', '')) != file_path]
            if len(new_list) != len(cat_list):
                folder_data[category] = new_list

        # 2. 从 favorites 中移除
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        for g in groups:
            items = g.get('items', [])
            g['items'] = [p for p in items if normalize_path(p) != file_path]
        favorites[category] = groups
        config['favorites'] = favorites

        save_config(config)
        return {'ok': True, 'msg': '已从软件中移除 (本地原文件未做任何改动)'}

    def delete_file_real(self, file_path: str, category: str) -> Dict[str, Any]:
        """
        【高危操作】永久删除本地真实文件
        先执行真实文件删除，再清理软件内的映射和收藏引用
        """
        if not file_path:
            return {'ok': False, 'msg': '路径为空'}
        file_path = normalize_path(file_path)

        if not os.path.exists(file_path):
            return {'ok': False, 'msg': '文件不存在'}
        if os.path.isdir(file_path):
            return {'ok': False, 'msg': '目标不是文件'}

        try:
            os.remove(file_path)
        except Exception as e:
            return {'ok': False, 'msg': f'删除失败: {e}'}

        # 清理映射和收藏
        self.delete_file_map(file_path, category)
        return {'ok': True, 'msg': '已永久删除本地文件'}

    def rename_file(self, file_path: str, new_name: str) -> Dict[str, Any]:
        """
        重命名本地文件，并同步更新收藏夹中的路径引用
        new_name: 新文件名（可含或不含扩展名）
        """
        if not file_path or not new_name:
            return {'ok': False, 'msg': '路径或新文件名为空'}
        file_path = normalize_path(file_path)
        if not os.path.exists(file_path):
            return {'ok': False, 'msg': '文件不存在'}
        if os.path.isdir(file_path):
            return {'ok': False, 'msg': '目标不是文件'}

        dir_path = os.path.dirname(file_path)
        old_name = os.path.basename(file_path)
        old_name_no_ext, old_ext = os.path.splitext(old_name)
        new_name = new_name.strip()

        # 如果用户没输入扩展名，保留原扩展名
        if '.' not in new_name or new_name.startswith('.'):
            new_name = new_name + old_ext

        # 处理同名冲突
        new_path = os.path.join(dir_path, new_name)
        original_new_path = new_path
        counter = 1
        name_part, ext_part = os.path.splitext(new_name)
        while os.path.exists(new_path) and normalize_path(new_path) != normalize_path(file_path):
            new_path = os.path.join(dir_path, f"{name_part}_{counter}{ext_part}")
            counter += 1

        try:
            os.rename(file_path, new_path)
        except Exception as e:
            return {'ok': False, 'msg': f'重命名失败: {e}'}

        # 同步更新收藏夹中的路径引用
        config = load_config()
        favorites = config.get('favorites', {})
        for cat in ['video', 'image', 'document', 'project']:
            groups = favorites.get(cat, [])
            for g in groups:
                items = g.get('items', [])
                for i, p in enumerate(items):
                    if normalize_path(p) == normalize_path(file_path):
                        items[i] = normalize_path(new_path)
        config['favorites'] = favorites
        save_config(config)

        return {'ok': True, 'msg': '重命名成功', 'new_path': normalize_path(new_path)}

    def batch_delete_map(self, file_paths: List[str], category: str) -> Dict[str, Any]:
        """批量映射删除"""
        if not file_paths:
            return {'ok': True, 'msg': '没有需要处理的文件'}
        success = 0
        failed = 0
        for p in file_paths:
            result = self.delete_file_map(p, category)
            if result.get('ok'):
                success += 1
            else:
                failed += 1
        return {'ok': failed == 0, 'msg': f'移除完成: 成功 {success} 个, 失败 {failed} 个'}

    def batch_delete_real(self, file_paths: List[str], category: str) -> Dict[str, Any]:
        """批量真实删除"""
        if not file_paths:
            return {'ok': True, 'msg': '没有需要处理的文件'}
        success = 0
        failed = 0
        for p in file_paths:
            result = self.delete_file_real(p, category)
            if result.get('ok'):
                success += 1
            else:
                failed += 1
        return {'ok': failed == 0, 'msg': f'删除完成: 成功 {success} 个, 失败 {failed} 个'}

    def batch_rename_files(self, file_paths: List[str], base_name: str) -> Dict[str, Any]:
        """批量重命名文件：base_name + _序号 + 原扩展名"""
        if not file_paths:
            return {'ok': True, 'msg': '没有需要处理的文件'}
        if not base_name or not base_name.strip():
            return {'ok': False, 'msg': '新文件名不能为空'}
        base_name = base_name.strip()

        config = load_config()
        favorites = config.get('favorites', {})
        moved = []
        failed = []

        for i, src_path in enumerate(file_paths, start=1):
            if not os.path.exists(src_path):
                failed.append({'path': src_path, 'reason': '源文件不存在'})
                continue
            dir_path = os.path.dirname(src_path)
            _, ext = os.path.splitext(src_path)
            new_name = f"{base_name}_{i}{ext}"
            new_path = os.path.join(dir_path, new_name)
            # 处理同名冲突
            c = 1
            while os.path.exists(new_path) and normalize_path(new_path) != normalize_path(src_path):
                new_path = os.path.join(dir_path, f"{base_name}_{i}_{c}{ext}")
                c += 1
            try:
                os.rename(src_path, new_path)
                moved.append({'old': src_path, 'new': new_path})
                # 同步更新收藏夹路径
                for cat in ['video', 'image', 'document', 'project']:
                    groups = favorites.get(cat, [])
                    for g in groups:
                        items = g.get('items', [])
                        for j, p in enumerate(items):
                            if normalize_path(p) == normalize_path(src_path):
                                items[j] = normalize_path(new_path)
            except Exception as e:
                failed.append({'path': src_path, 'reason': str(e)})

        config['favorites'] = favorites
        save_config(config)
        return {
            'ok': len(moved) > 0,
            'msg': f'重命名完成: 成功 {len(moved)} 个, 失败 {len(failed)} 个',
            'moved': moved,
            'failed': failed
        }

    def batch_add_favorites(self, category: str, group_id: str, file_paths: List[str]) -> Dict[str, Any]:
        """批量添加到收藏夹"""
        if not file_paths:
            return {'ok': True, 'msg': '没有需要处理的文件'}
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        target_group = None
        for g in groups:
            if g.get('id') == group_id:
                target_group = g
                break
        if not target_group:
            return {'ok': False, 'msg': '收藏夹不存在'}

        items = target_group.get('items', [])
        added = 0
        skipped = 0
        for p in file_paths:
            np = normalize_path(p)
            if np not in items:
                items.append(np)
                added += 1
            else:
                skipped += 1
        target_group['items'] = items
        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': f'收藏完成: 新增 {added} 个, 已存在 {skipped} 个'}

    def batch_remove_favorites(self, category: str, file_paths: List[str]) -> Dict[str, Any]:
        """批量从收藏夹移除 (不指定 group_id, 从所有收藏夹中移除)"""
        if not file_paths:
            return {'ok': True, 'msg': '没有需要处理的文件'}
        config = load_config()
        favorites = config.get('favorites', {})
        groups = favorites.get(category, [])
        removed = 0

        norm_paths = [normalize_path(p) for p in file_paths]
        for g in groups:
            items = g.get('items', [])
            original_len = len(items)
            g['items'] = [p for p in items if normalize_path(p) not in norm_paths]
            removed += original_len - len(g['items'])

        favorites[category] = groups
        config['favorites'] = favorites
        save_config(config)
        return {'ok': True, 'msg': f'取消收藏完成: 共移除 {removed} 条'}

    def search_files(self, keyword: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        全局模糊搜索文件 (基于 scan_cache)
        返回: {video: [...], image: [...], document: [...]}
        """
        if not keyword:
            return {'video': [], 'image': [], 'document': []}
        keyword_lower = keyword.lower()
        config = load_config()
        scan_cache = config.get('scan_cache', {})
        favorites = config.get('favorites', {})

        # 构建已收藏路径集合
        fav_sets: Dict[str, set] = {}
        for cat in ['video', 'image', 'document']:
            fav_sets[cat] = set()
            for g in favorites.get(cat, []):
                for p in g.get('items', []):
                    fav_sets[cat].add(normalize_path(p))

        result: Dict[str, List[Dict[str, Any]]] = {'video': [], 'image': [], 'document': []}
        seen: Dict[str, set] = {'video': set(), 'image': set(), 'document': set()}

        for folder_data in scan_cache.values():
            for cat in ['video', 'image', 'document']:
                for item in folder_data.get(cat, []):
                    path = item.get('path', '')
                    name = item.get('name', '')
                    norm_path = normalize_path(path)
                    if norm_path in seen[cat]:
                        continue
                    if keyword_lower in name.lower():
                        seen[cat].add(norm_path)
                        item_copy = dict(item)
                        item_copy['is_favorited'] = norm_path in fav_sets[cat]
                        result[cat].append(item_copy)

        # 按修改时间倒序
        for cat in result:
            result[cat].sort(key=lambda x: x.get('modified', 0), reverse=True)

        return result


# ============================================================
# 五、主入口
# ============================================================

def main() -> None:
    """程序入口"""
    # 启动前先把数据目录建好, 防止首次启动 JSON 读写报错
    ensure_data_dir()

    # 实例化 API 桥接对象
    api = Api()

    # 加载前端 HTML (单文件, 内含全部 CSS 与 JS)
    html_path = resource_path('ui.html')

    # 创建桌面窗口
    window = webview.create_window(
        title=f"{APP_TITLE} v{APP_VERSION}",
        url=html_path,
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 640),
        resizable=True,    # 自由缩放
        text_select=True,  # 允许文本选中复制
    )

    # 把窗口引用注入 Api, 让 pick_folder 等方法能拿到
    # 注意: 使用 _ 开头的方法名, 这样 pywebview 不会把它暴露到前端
    api._set_window(window)

    # --------------------------------------------------------
    # 拦截 WebView2 拖拽文件夹时的默认行为
    # pywebview 6.x + EdgeChromium 下, 拖拽文件夹到窗口会触发两类事件:
    #   1) NewWindowRequested → pywebview 默认在外部浏览器打开 (图1)
    #   2) NavigationStarting → WebView2 内部导航到 file:// 索引页
    # JS 层面的 preventDefault 拦不住这两类底层行为, 必须在 Python 端拦截。
    # --------------------------------------------------------
    try:
        from webview.platforms.edgechromium import EdgeChrome
        _orig_nav_start = EdgeChrome.on_navigation_start
        _orig_new_win = EdgeChrome.on_new_window_request

        def _extract_drag_path(uri: str) -> str:
            """从 file:// URI 提取本地绝对路径"""
            path = uri.replace('file:///', '').replace('file://', '')
            try:
                import urllib.parse
                path = urllib.parse.unquote(path)
            except Exception:
                pass
            return path

        def _notify_frontend_drop(window, paths: list):
            """异步通知前端处理拖拽路径, 避免在 WebView2 同步回调中死锁"""
            if not window:
                return

            def _async():
                paths_json = json.dumps(paths)
                js = (
                    "(function(){"
                    "_dragDepth=0;"
                    "if(typeof state!=='undefined')state.dragOver=false;"
                    "var gd=document.getElementById('globalDrop');"
                    "if(gd)gd.innerHTML='';"
                    "if(window.__onExternalDrop)window.__onExternalDrop(" + paths_json + ");"
                    "})();"
                )
                try:
                    window.evaluate_js(js)
                except Exception:
                    pass

            threading.Thread(target=_async, daemon=True).start()

        def _patched_on_navigation_start(self, sender, args):
            uri = str(args.Uri)
            if uri.startswith('file://'):
                args.Cancel = True
                _notify_frontend_drop(self.pywebview_window, [_extract_drag_path(uri)])
                return
            return _orig_nav_start(self, sender, args)

        def _patched_on_new_window_request(self, sender, args):
            uri = str(args.get_Uri())
            if uri.startswith('file://'):
                args.set_Handled(True)
                _notify_frontend_drop(self.pywebview_window, [_extract_drag_path(uri)])
                return
            return _orig_new_win(self, sender, args)

        EdgeChrome.on_navigation_start = _patched_on_navigation_start
        EdgeChrome.on_new_window_request = _patched_on_new_window_request
    except Exception:
        pass  # 非 EdgeChromium 后端时静默跳过

    # 启动事件循环 (阻塞)
    # debug=False 关闭右键菜单/调试工具, 让外观更像正式软件
    # 想调试时改成 debug=True 即可
    webview.start(debug=False)


# 直接运行入口
if __name__ == '__main__':
    main()
