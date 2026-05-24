#!/usr/bin/env python3
"""医美门诊客户照片管理系统 - 支持管理员和只读账户"""

import os
import sqlite3
import datetime
import uuid
import shutil
import hashlib
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
MAX_FILE_SIZE = 100 * 1024 * 1024
SESSION_KEY = os.environ.get('SESSION_KEY', 'clinic_photos_secret_key_2025')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.secret_key = SESSION_KEY


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
        # 账户格式: 用户名|密码哈希|角色  (role: admin / viewer)
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
            # 新格式: 用户名|密码哈希|角色
            if len(parts) == 3:
                stored_user, stored_hash, role = parts
                if stored_user == username and stored_hash == pwd_hash:
                    return role
            # 兼容旧格式: 密码哈希|角色
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
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_photo_datetime(filepath):
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
# 扫描已有照片
# ============================================================
def scan_existing_photos():
    conn = get_db()
    existing = set(row[0] for row in conn.execute('SELECT file_path FROM photos').fetchall())
    imported, skipped, errors = 0, 0, 0

    if not os.path.isdir(PHOTOS_DIR):
        return imported, skipped, errors

    for root, dirs, files in os.walk(PHOTOS_DIR):
        if '.thumbs' in root.split(os.sep):
            continue
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, PHOTOS_DIR).replace('\\', '/')
            if rel_path in existing:
                skipped += 1
                continue
            try:
                file_size = os.path.getsize(abs_path)
                if file_size < 1024:
                    continue
                photo_dt = get_photo_datetime(abs_path)
                photo_date = photo_dt.date()
                photo_time = photo_dt.strftime('%H:%M:%S')
                parts = Path(rel_path).parts
                customer_name = '未分类'
                if len(parts) >= 5:
                    customer_name = parts[3]
                elif len(parts) >= 4 and not parts[2].isdigit():
                    customer_name = parts[2]
                elif len(parts) >= 2 and not parts[0].isdigit():
                    customer_name = parts[0]
                thumb_rel_dir = os.path.join('.thumbs', os.path.dirname(rel_path))
                thumb_filename = fname.rsplit('.', 1)[0] + '.jpg'
                thumb_path = os.path.join(PHOTOS_DIR, thumb_rel_dir, thumb_filename)
                create_thumbnail(abs_path, thumb_path)
                upload_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                conn.execute('''
                    INSERT INTO photos (customer_name, file_path, photo_date, photo_time,
                                        upload_time, file_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (customer_name, rel_path, photo_date.strftime('%Y-%m-%d'),
                      photo_time, upload_time, file_size))
                existing.add(rel_path)
                imported += 1
            except Exception as e:
                errors += 1

    conn.commit()
    conn.close()
    return imported, skipped, errors


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
    for r in rows:
        d = r['photo_date']
        if d not in grouped:
            grouped[d] = {}
        name = r['customer_name']
        if name not in grouped[d]:
            grouped[d][name] = []
        grouped[d][name].append(dict(r))
    conn.close()
    return render_template('search.html',
                           results=grouped,
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
    imported, skipped, errors = scan_existing_photos()
    return jsonify({'imported': imported, 'skipped': skipped, 'errors': errors})


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
    abs_path = os.path.join(PHOTOS_DIR, '.thumbs', filepath)
    if not os.path.exists(abs_path):
        abs_path = os.path.join(PHOTOS_DIR, filepath)
        if not os.path.exists(abs_path):
            abort(404)
    return send_file(abs_path)


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

    disk_count = 0
    if os.path.isdir(PHOTOS_DIR):
        for root, dirs, files in os.walk(PHOTOS_DIR):
            if '.thumbs' in root.split(os.sep):
                continue
            for f in files:
                if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
                    disk_count += 1

    return jsonify({
        'total_photos': total_photos,
        'total_customers': total_customers,
        'disk_files': disk_count,
        'unscanned': max(0, disk_count - total_photos)
    })


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
