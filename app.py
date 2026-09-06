#!/usr/bin/env python3
"""医美门诊客户照片管理系统 - 支持管理员和只读账户"""

import os
import re
import time
import sqlite3
import datetime
import uuid
import shutil
import hashlib
import threading
import json
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_file, abort, session)

from PIL import Image

# --- 配置 ---
PHOTOS_DIR = os.environ.get('PHOTOS_DIR', '/photos')
DATA_DIR = os.environ.get('DATA_DIR', '/data')
THUMBS_DIR = os.path.join(PHOTOS_DIR, '.thumbs')
THUMB_SIZE = (600, 600)
DATABASE = os.path.join(DATA_DIR, 'clinic.db')
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', '3gp', 'flv', 'wmv', 'ts', 'm2ts', 'mpg', 'mpeg'}
SUPPORTED_EXTENSIONS = ALLOWED_EXTENSIONS | VIDEO_EXTENSIONS
MAX_FILE_SIZE = 2048 * 1024 * 1024  # 2GB，支持大视频
SESSION_KEY = os.environ.get('SESSION_KEY', 'clinic_photos_secret_key_2025')
SCAN_STATE_FILE = os.path.join(DATA_DIR, '.scan_state')
DISK_COUNT_FILE = os.path.join(DATA_DIR, '.disk_count')
DB_BATCH_SIZE = 2000

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.secret_key = SESSION_KEY

_scan_lock = threading.Lock()


# ============================================================
# 认证系统 - 支持管理员(admin)和只读(viewer)
# ============================================================
def hash_password(password):
    return hashlib.sha256(f'clinic_{password}'.encode()).hexdigest()


def get_accounts_file():
    return os.path.join(DATA_DIR, '.accounts')


def init_accounts():
    """初始化默认账户: admin/admin, xyym/1766(viewer)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    f = get_accounts_file()
    if not os.path.exists(f):
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write(f"admin|{hash_password('admin')}|admin\n")
            fh.write(f"xyym|{hash_password('1766')}|viewer\n")


def verify_account(username, password):
    """验证账户，返回角色(role)或None"""
    f = get_accounts_file()
    if not os.path.exists(f):
        return None

    pwd_hash = hashlib.sha256(f'clinic_{password}'.encode()).hexdigest()

    with open(f, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) == 3:
                stored_user, stored_hash, role = parts
                if stored_user == username and stored_hash == pwd_hash:
                    return role
            elif len(parts) == 2:
                stored_hash, role = parts
                if stored_hash == pwd_hash:
                    return role

    return None


def get_all_accounts():
    """获取所有账户信息，返回 [(username, role), ...]"""
    f = get_accounts_file()
    accounts = []
    if os.path.exists(f):
        with open(f, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) == 3:
                    accounts.append((parts[0], parts[2]))
                elif len(parts) == 2:
                    accounts.append(('', parts[1]))
    return accounts


def change_password(username, new_password):
    """修改指定用户的密码"""
    f = get_accounts_file()
    lines = []
    found = False
    if os.path.exists(f):
        with open(f, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) == 3 and parts[0] == username:
                    lines.append(f"{username}|{hash_password(new_password)}|{parts[2]}\n")
                    found = True
                else:
                    lines.append(line + '\n')
    if not found:
        lines.append(f"{username}|{hash_password(new_password)}|admin\n")

    with open(f, 'w', encoding='utf-8') as fh:
        fh.writelines(lines)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """要求管理员权限"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ============================================================
# 数据库
# ============================================================
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(THUMBS_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            photo_date TEXT NOT NULL,
            photo_time TEXT,
            upload_time TEXT DEFAULT (datetime('now','localtime')),
            file_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_photos_customer ON photos(customer_name);
        CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(photo_date);
        CREATE INDEX IF NOT EXISTS idx_photos_name_date ON photos(customer_name, photo_date);
    ''')
    conn.commit()
    conn.close()


# ============================================================
# 工具函数
# ============================================================
def allowed_file(filename):
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in SUPPORTED_EXTENSIONS


def is_video_file(path):
    """判断文件是否为视频（供模板使用）"""
    if '.' not in path:
        return False
    ext = path.rsplit('.', 1)[1].lower()
    return ext in VIDEO_EXTENSIONS


def get_photo_datetime(filepath):
    """从 EXIF 获取照片拍摄时间，用于上传路由"""
    try:
        img = Image.open(filepath)
        exif = img.getexif()
        for tag_id in (36867, 36868, 306):
            if tag_id in exif:
                date_str = exif[tag_id]
                if isinstance(date_str, str) and ':' in date_str:
                    return datetime.datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
    except Exception:
        return datetime.datetime.now()


def create_thumbnail(filepath, thumb_path):
    try:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with Image.open(filepath) as img:
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            if img.mode in ('RGBA', 'P', 'LA'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(thumb_path, 'JPEG', quality=85, optimize=True)
    except Exception as e:
        print(f"缩略图生成失败: {e}")


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ============================================================
# 路径解析 - 从目录结构提取客户名和日期
# ============================================================
# 预编译正则表达式（性能优化）
_re_digits_only = re.compile(r'^[\d_\-\.]+$')
_re_month = re.compile(r'^\d+月')
_re_filename_date = re.compile(r'^(\d{4})[_\-](\d{2})[_\-](\d{2})')
_re_month_after = re.compile(r'^(\d+)月')

def _is_name_like(s):
    """判断目录名是否像客户姓名（而非日期或系统目录）"""
    if not s or len(s) > 50:
        return False
    if s in ('术前', '术后', '.thumbs', '新建文件夹'):
        return False
    if _re_digits_only.match(s):
        return False
    if _re_month.match(s):
        return False
    if re.match(r'^\d{4}', s):
        return False
    if '平板' in s or '备份' in s:
        return False
    if s[0] == '.':
        return False
    return True


def _extract_customer_name(parts):
    """从路径中提取客户姓名，从文件名向前回溯找到第一个像人名的目录"""
    for i in range(len(parts) - 2, -1, -1):
        if _is_name_like(parts[i]):
            return parts[i]
    return '未分类'


def _extract_date_from_path(parts):
    """从目录结构提取日期。

    支持格式:
    - YYYY/MM/DD/...          (2024-2026 标准格式)
    - YYYY/X月之后/X.X/...   (2023 旧格式, 如 10月之后/10.1)
    - YYYY/MM/X.X/...        (混合格式, 如 01/1.28)
    """
    year = None
    year_idx = -1
    for i, p in enumerate(parts):
        if p.isdigit() and len(p) == 4 and 2020 <= int(p) <= 2030:
            year = int(p)
            year_idx = i
            break

    if year is None:
        return None

    month = None
    day = None

    for j in range(year_idx + 1, len(parts)):
        d = parts[j]

        # "X月之后" 或 "X月" 格式
        m = _re_month_after.match(d)
        if m:
            month = int(m.group(1))
            continue

        # 数字格式（可能含点号，如 "1.10"、"24"）
        clean = d.replace('.', '')
        if clean.isdigit():
            num = int(clean)
            if month is None and 1 <= num <= 12:
                month = num
                continue
            if month is not None and day is None:
                # 提取日期（处理 "X.Y" 格式，取点号后面的数字作为日期）
                if '.' in d:
                    day = int(d.split('.')[-1])
                else:
                    day = num
                if 1 <= day <= 31:
                    break
        elif month is not None:
            # 遇到非数字目录，停止日期查找
            break

    if month and day:
        try:
            return datetime.date(year, month, day)
        except ValueError:
            pass
    return None


def _extract_date_from_filename(filename):
    """从文件名中提取日期，如 '2026_05_02_09_06_IMG_6661.jpg'"""
    m = _re_filename_date.match(filename)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _extract_metadata_fast(abs_path, rel_path, mtime):
    """高性能版 _extract_metadata，直接接收 mtime 避免 stat 重复调用。"""
    parts = Path(rel_path).parts

    customer_name = _extract_customer_name(parts)
    photo_date = _extract_date_from_path(parts)

    if not photo_date:
        photo_date = _extract_date_from_filename(parts[-1])

    try:
        dt = datetime.datetime.fromtimestamp(mtime)
        if not photo_date:
            photo_date = dt.date()
        photo_time = dt.strftime('%H:%M:%S')
    except Exception:
        if not photo_date:
            photo_date = datetime.date.today()
        photo_time = '00:00:00'

    return customer_name, photo_date, photo_time


def _extract_metadata(abs_path, rel_path):
    """从文件路径提取 (客户姓名, 拍摄日期, 拍摄时间)。

    优先级: 目录结构 > 文件名 > 文件修改时间
    """
    parts = Path(rel_path).parts

    customer_name = _extract_customer_name(parts)

    # 从目录结构提取日期
    photo_date = _extract_date_from_path(parts)

    # 回退: 从文件名提取
    if not photo_date:
        photo_date = _extract_date_from_filename(parts[-1])

    # 回退: 文件修改时间
    try:
        mt = os.path.getmtime(abs_path)
        if not photo_date:
            photo_date = datetime.datetime.fromtimestamp(mt).date()
        photo_time = datetime.datetime.fromtimestamp(mt).strftime('%H:%M:%S')
    except Exception:
        if not photo_date:
            photo_date = datetime.date.today()
        photo_time = '00:00:00'

    return customer_name, photo_date, photo_time


# ============================================================
# 扫描状态管理
# ============================================================
def _update_scan_state(**kwargs):
    with _scan_lock:
        kwargs['updated'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(SCAN_STATE_FILE, 'w') as f:
            json.dump(kwargs, f, ensure_ascii=False)


def _get_scan_state():
    try:
        if os.path.exists(SCAN_STATE_FILE):
            with open(SCAN_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {'running': False, 'imported': 0, 'skipped': 0, 'errors': 0, 'scanned': 0}


def _get_disk_count():
    """读取缓存的照片总数"""
    try:
        if os.path.exists(DISK_COUNT_FILE):
            with open(DISK_COUNT_FILE) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return 0


def _update_disk_count():
    """统计磁盘上的照片总数并缓存"""
    count = 0
    if os.path.isdir(PHOTOS_DIR):
        for root, dirs, files in os.walk(PHOTOS_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            count += sum(1 for f in files
                         if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS)
    with open(DISK_COUNT_FILE, 'w') as f:
        f.write(str(count))
    return count

def scan_existing_photos():
    """扫描 PHOTOS_DIR 下所有照片，导入到数据库（高性能批量版）。

    兼容多种目录格式:
    - 2024-2026: YYYY/MM/DD/客户名/photo.jpg
    - 2023: YYYY/X月之后/X.X/客户名/photo.jpg
    - 平板备份: YYYY/平板内照片.../photo.jpg -> 归为"未分类"
    - 散落文件: 5-2/, WIN-F8468AN6QKE/ -> 归为"未分类"

    缩略图采用懒加载策略，扫描时不生成，首次查看时按需生成。

    性能优化:
    - 批量 INSERT（executemany）减少 SQLite 事务开销
    - 时间窗口状态更新（每 3 秒一次）替代逐目录更新
    - 预编译正则替代运行时编译
    - 一次性 os.stat 获取 size+mtime 避免重复系统调用
    """
    conn = get_db()
    existing = set(row[0] for row in conn.execute('SELECT file_path FROM photos').fetchall())
    conn.close()

    imported, skipped, errors, scanned = 0, 0, 0, 0
    batch_records = []
    last_status_time = 0.0
    now_str_cache = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    _update_scan_state(running=True, imported=0, skipped=0, errors=0, scanned=0)

    try:
        if not os.path.isdir(PHOTOS_DIR):
            _update_scan_state(running=False, imported=0, skipped=0, errors=0,
                               scanned=0, message='照片目录不存在')
            return

        conn = get_db()

        for root, dirs, files in os.walk(PHOTOS_DIR):
            # 跳过隐藏目录（.thumbs, .DS_Store 等）
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for fname in files:
                scanned += 1

                # 快速扩展名过滤
                dot = fname.rfind('.')
                if dot < 1:
                    continue
                ext = fname[dot:].lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue

                abs_path = os.path.join(root, fname)
                rel_path = abs_path.replace(PHOTOS_DIR, '').lstrip(os.sep).replace('\\', '/')

                # 已在数据库中，跳过
                if rel_path in existing:
                    skipped += 1
                    continue

                try:
                    # 一次性获取 stat 信息（size + mtime）
                    st = os.stat(abs_path)
                    file_size = st.st_size
                    if file_size < 1024:
                        continue

                    customer_name, photo_date, photo_time = _extract_metadata_fast(
                        abs_path, rel_path, st.st_mtime
                    )

                    batch_records.append((
                        customer_name, rel_path,
                        photo_date.strftime('%Y-%m-%d'), photo_time,
                        now_str_cache, file_size
                    ))
                    existing.add(rel_path)
                    imported += 1

                    # 批量提交: 每 DB_BATCH_SIZE 条
                    if len(batch_records) >= DB_BATCH_SIZE:
                        conn.executemany('''
                            INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                                upload_time, file_size)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', batch_records)
                        conn.commit()
                        batch_records = []

                except Exception as e:
                    errors += 1
                    if errors <= 20:
                        print(f'扫描错误: {rel_path} -> {e}')

            # 时间窗口状态更新（每 3 秒一次）
            current_time = time.time()
            if current_time - last_status_time >= 3.0:
                last_status_time = current_time
                if batch_records:
                    conn.executemany('''
                        INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                            upload_time, file_size)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', batch_records)
                    conn.commit()
                    batch_records = []
                _update_scan_state(running=True, imported=imported,
                                   skipped=skipped, errors=errors, scanned=scanned)

        # 最终提交剩余记录
        if batch_records:
            conn.executemany('''
                INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                    upload_time, file_size)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', batch_records)
            conn.commit()

        conn.close()

        # 更新磁盘缓存数量
        _update_disk_count()

        _update_scan_state(running=False, imported=imported, skipped=skipped,
                           errors=errors, scanned=scanned,
                           message=f'扫描完成: 导入 {imported:,} 张, '
                                   f'跳过 {skipped:,} 张, 错误 {errors} 张')

    except Exception as e:
        # 出错前尽量提交已收集的记录
        if batch_records:
            try:
                conn.executemany('''
                    INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                        upload_time, file_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', batch_records)
                conn.commit()
            except Exception:
                pass
        _update_scan_state(running=False, imported=imported, skipped=skipped,
                           errors=errors, scanned=scanned,
                           message=f'扫描异常中断: {str(e)}')
        print(f'扫描异常: {e}')


# ============================================================
# 路由 - 认证
# ============================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = verify_account(username, password)
        if role:
            session['logged_in'] = True
            session['username'] = username
            session['role'] = role
            session['login_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            next_page = request.args.get('next', '/')
            return redirect(next_page)
        return render_template('login.html', error='账号或密码错误')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/change_password', methods=['GET', 'POST'])
@login_required
@admin_required
def change_password():
    if request.method == 'POST':
        old_pwd = request.form.get('old_password', '')
        new_pwd = request.form.get('new_password', '')
        confirm_pwd = request.form.get('confirm_password', '')

        # 验证旧密码
        old_role = verify_account(session.get('username', ''), old_pwd)
        if not old_role:
            return render_template('change_password.html', error='原密码错误')
        if len(new_pwd) < 4:
            return render_template('change_password.html', error='新密码至少4个字符')
        if new_pwd != confirm_pwd:
            return render_template('change_password.html', error='两次密码不一致')

        change_password(session['username'], new_pwd)
        session.clear()
        return render_template('login.html', msg='密码已修改，请重新登录')

    return render_template('change_password.html')


# ============================================================
# 路由 - 查看类（admin + viewer 均可访问）
# ============================================================
@app.route('/')
@login_required
def index():
    today = datetime.date.today()
    return redirect(url_for('view_date', year=today.year, month=today.month, day=today.day))


@app.route('/d/<int:year>/<int:month>/<int:day>')
@login_required
def view_date(year, month, day):
    try:
        date = datetime.date(year, month, day)
    except ValueError:
        abort(404)

    date_str = date.strftime('%Y-%m-%d')
    conn = get_db()
    photos = conn.execute('''
        SELECT * FROM photos WHERE photo_date = ?
        ORDER BY customer_name, photo_time DESC
    ''', (date_str,)).fetchall()

    customers = {}
    for p in photos:
        name = p['customer_name']
        if name not in customers:
            customers[name] = []
        customers[name].append(dict(p))

    customer_count = len(customers)
    photo_count = len(photos)
    conn.close()

    prev_day = date - datetime.timedelta(days=1)
    next_day = date + datetime.timedelta(days=1)
    today = datetime.date.today()

    return render_template('date.html',
                           date=date, today=today,
                           customers=customers,
                           customer_count=customer_count,
                           photo_count=photo_count,
                           prev_day=prev_day, next_day=next_day)


@app.route('/customer/<name>')
@login_required
def customer(name):
    conn = get_db()
    photos = conn.execute('''
        SELECT * FROM photos WHERE customer_name = ?
        ORDER BY photo_date DESC, photo_time DESC
    ''', (name,)).fetchall()
    total_photos = len(photos)
    dates = set(p['photo_date'] for p in photos)
    by_date = {}
    for p in photos:
        d = p['photo_date']
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(dict(p))
    by_date = dict(sorted(by_date.items(), reverse=True))
    conn.close()
    return render_template('customer.html', name=name,
                           by_date=by_date,
                           total_photos=total_photos,
                           total_dates=len(dates))


@app.route('/customers')
@login_required
def customers():
    q = request.args.get('q', '').strip()
    conn = get_db()
    if q:
        rows = conn.execute('''
            SELECT customer_name, COUNT(*) as photo_count,
                   MIN(photo_date) as first_seen, MAX(photo_date) as last_seen
            FROM photos WHERE customer_name LIKE ?
            GROUP BY customer_name ORDER BY customer_name
        ''', (f'%{q}%',)).fetchall()
    else:
        rows = conn.execute('''
            SELECT customer_name, COUNT(*) as photo_count,
                   MIN(photo_date) as first_seen, MAX(photo_date) as last_seen
            FROM photos GROUP BY customer_name
            ORDER BY MAX(upload_time) DESC
        ''').fetchall()
    conn.close()
    return render_template('customers.html', customers=rows, q=q)


@app.route('/search')
@login_required
def search():
    q = request.args.get('q', '').strip()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()
    conn = get_db()
    conditions, params = [], []
    if q:
        conditions.append('customer_name LIKE ?')
        params.append(f'%{q}%')
    if date_from:
        conditions.append('photo_date >= ?')
        params.append(date_from)
    if date_to:
        conditions.append('photo_date <= ?')
        params.append(date_to)
    where = ' AND '.join(conditions) if conditions else '1=1'
    rows = conn.execute(f'''
        SELECT * FROM photos WHERE {where}
        ORDER BY photo_date DESC, customer_name, photo_time DESC LIMIT 200
    ''', params).fetchall()
    grouped = {}
    day_counts = {}
    for r in rows:
        d = r['photo_date']
        if d not in grouped:
            grouped[d] = {}
            day_counts[d] = 0
        name = r['customer_name']
        if name not in grouped[d]:
            grouped[d][name] = []
        grouped[d][name].append(dict(r))
        day_counts[d] += 1
    conn.close()
    return render_template('search.html',
                           results=grouped,
                           day_counts=day_counts,
                           q=q, date_from=date_from, date_to=date_to,
                           total=len(rows))


# ============================================================
# 路由 - 管理类（仅 admin 可访问）
# ============================================================
@app.route('/upload', methods=['GET', 'POST'])
@admin_required
def upload():
    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        if not customer_name:
            return redirect(url_for('upload'))
        files = request.files.getlist('photos')
        if not files or all(f.filename == '' for f in files):
            return redirect(url_for('upload'))

        conn = get_db()
        saved_count = 0
        last_photo_date = datetime.date.today()
        upload_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        temp_dir = os.path.join(DATA_DIR, 'temp_upload')
        os.makedirs(temp_dir, exist_ok=True)

        for file in files:
            if not file or not allowed_file(file.filename):
                continue
            temp_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}")
            file.save(temp_path)
            photo_dt = get_photo_datetime(temp_path)
            photo_date = photo_dt.date()
            photo_time = photo_dt.strftime('%H:%M:%S')
            last_photo_date = photo_date

            ext = file.filename.rsplit('.', 1)[1].lower()
            time_prefix = photo_dt.strftime('%H%M%S')
            unique_name = f"{time_prefix}_{uuid.uuid4().hex[:8]}.{ext}"
            rel_dir = os.path.join(str(photo_date.year), f"{photo_date.month:02d}",
                                   f"{photo_date.day:02d}", customer_name)
            abs_dir = os.path.join(PHOTOS_DIR, rel_dir)
            os.makedirs(abs_dir, exist_ok=True)
            final_path = os.path.join(abs_dir, unique_name)
            shutil.move(temp_path, final_path)

            if ext in ALLOWED_EXTENSIONS:
                thumb_rel_dir = os.path.join('.thumbs', rel_dir)
                thumb_filename = unique_name.rsplit('.', 1)[0] + '.jpg'
                thumb_path = os.path.join(PHOTOS_DIR, thumb_rel_dir, thumb_filename)
                create_thumbnail(final_path, thumb_path)

            file_size = os.path.getsize(final_path)
            rel_path = rel_dir + '/' + unique_name
            conn.execute('''
                INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                    upload_time, file_size)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (customer_name, rel_path, photo_date.strftime('%Y-%m-%d'),
                  photo_time, upload_time, file_size))
            saved_count += 1

        conn.commit()
        conn.close()
        return redirect(url_for('view_date', year=last_photo_date.year,
                                month=last_photo_date.month, day=last_photo_date.day))

    conn = get_db()
    recent_names = conn.execute('''
        SELECT DISTINCT customer_name FROM photos ORDER BY id DESC LIMIT 50
    ''').fetchall()
    conn.close()
    return render_template('upload.html', recent_names=[r['customer_name'] for r in recent_names])


@app.route('/scan')
@admin_required
def scan_photos():
    """启动后台扫描，立即返回"""
    state = _get_scan_state()
    if state.get('running'):
        return jsonify(state)

    t = threading.Thread(target=scan_existing_photos, daemon=True)
    t.start()

    return jsonify({
        'running': True, 'imported': 0, 'skipped': 0,
        'errors': 0, 'scanned': 0, 'message': '扫描已开始'
    })


@app.route('/api/scan_status')
@login_required
def scan_status():
    """查询扫描进度"""
    state = _get_scan_state()
    # 检测过期状态（超过 5 分钟未更新视为已停止）
    if state.get('running') and 'updated' in state:
        try:
            updated = datetime.datetime.strptime(state['updated'], '%Y-%m-%d %H:%M:%S')
            if (datetime.datetime.now() - updated).total_seconds() > 300:
                state['running'] = False
                state['message'] = '扫描可能已中断（超过 5 分钟未更新）'
                _update_scan_state(**state)
        except Exception:
            pass
    return jsonify(state)


@app.route('/delete/<int:photo_id>', methods=['POST'])
@admin_required
def delete_photo(photo_id):
    conn = get_db()
    photo = conn.execute('SELECT * FROM photos WHERE id = ?', (photo_id,)).fetchone()
    if not photo:
        conn.close()
        abort(404)

    abs_path = os.path.join(PHOTOS_DIR, photo['file_path'])
    if os.path.exists(abs_path):
        os.remove(abs_path)
    thumb_dir = os.path.join(PHOTOS_DIR, '.thumbs', os.path.dirname(photo['file_path']))
    if os.path.exists(thumb_dir):
        base = os.path.basename(photo['file_path']).rsplit('.', 1)[0]
        for ext in ['jpg', 'png']:
            tp = os.path.join(thumb_dir, base + '.' + ext)
            if os.path.exists(tp):
                os.remove(tp)

    conn.execute('DELETE FROM photos WHERE id = ?', (photo_id,))
    conn.commit()
    conn.close()

    try:
        parent = os.path.dirname(abs_path)
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
            grandparent = os.path.dirname(parent)
            if os.path.isdir(grandparent) and not os.listdir(grandparent):
                os.rmdir(grandparent)
    except Exception:
        pass

    return redirect(request.headers.get('Referer', '/'))


# ============================================================
# 静态文件
# ============================================================
@app.route('/p/<path:filepath>')
@login_required
def serve_photo(filepath):
    abs_path = os.path.join(PHOTOS_DIR, filepath)
    if not os.path.exists(abs_path):
        abort(404)
    return send_file(abs_path)


@app.route('/t/<path:filepath>')
@login_required
def serve_thumb(filepath):
    """提供缩略图，如不存在则按需生成（懒加载）"""
    thumb_abs = os.path.join(PHOTOS_DIR, '.thumbs', filepath)

    if not os.path.exists(thumb_abs):
        # 找到原始照片文件
        thumb_base = os.path.splitext(os.path.basename(filepath))[0]
        photo_rel_dir = os.path.dirname(filepath)
        photo_dir_abs = os.path.join(PHOTOS_DIR, photo_rel_dir)

        found_original = None
        if os.path.isdir(photo_dir_abs):
            for ext in ALLOWED_EXTENSIONS:
                candidate = os.path.join(photo_dir_abs, thumb_base + '.' + ext)
                if os.path.exists(candidate):
                    found_original = candidate
                    break

        if found_original:
            os.makedirs(os.path.dirname(thumb_abs), exist_ok=True)
            create_thumbnail(found_original, thumb_abs)

    if os.path.exists(thumb_abs):
        return send_file(thumb_abs)

    # 缩略图生成失败，回退到原图
    abs_path = os.path.join(PHOTOS_DIR, filepath)
    if os.path.exists(abs_path):
        return send_file(abs_path)
    abort(404)


# ============================================================
# API
# ============================================================
@app.route('/api/search')
@login_required
def api_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    conn = get_db()
    rows = conn.execute('''
        SELECT DISTINCT customer_name FROM photos WHERE customer_name LIKE ?
        ORDER BY customer_name LIMIT 20
    ''', (f'%{q}%',)).fetchall()
    conn.close()
    return jsonify([r['customer_name'] for r in rows])


@app.route('/api/stats')
@login_required
def api_stats():
    conn = get_db()
    total_photos = conn.execute('SELECT COUNT(*) as c FROM photos').fetchone()['c']
    total_customers = conn.execute('SELECT COUNT(DISTINCT customer_name) as c FROM photos').fetchone()['c']
    conn.close()

    disk_count = _get_disk_count()

    return jsonify({
        'total_photos': total_photos,
        'total_customers': total_customers,
        'disk_files': disk_count,
        'unscanned': max(0, disk_count - total_photos)
    })


@app.route('/api/count_disk')
@admin_required
def count_disk():
    """手动触发磁盘照片计数（较慢）"""
    count = _update_disk_count()
    return jsonify({'disk_files': count})


# ============================================================
# 错误页
# ============================================================
@app.errorhandler(403)
def forbidden(e):
    return render_template('forbidden.html'), 403


# ============================================================
# 模板上下文
# ============================================================
@app.context_processor
def inject_globals():
    return {
        'format_size': format_size,
        'is_video': is_video_file,
        'now': datetime.datetime.now(),
        'logged_in': session.get('logged_in', False),
        'username': session.get('username', ''),
        'role': session.get('role', ''),
        'is_admin': session.get('role') == 'admin',
    }


# ============================================================
# 启动
# ============================================================
init_accounts()
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
