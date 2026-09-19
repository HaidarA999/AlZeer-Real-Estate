from flask import Flask, request, jsonify, send_from_directory, session, redirect, abort
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
import json
import secrets
from datetime import datetime, timedelta
import os
import re
import urllib.error
import urllib.parse
import urllib.request

app = Flask(__name__)

# مفتاح تشفير الجلسات (الـ session). بالإنتاج (production) لازم تحط
# متغير بيئة SECRET_KEY ثابت، لأنه لو ما ثبتناه، كل ما يعاد تشغيل
# السيرفر بيتغير المفتاح وبينفصل كل المسجلين دخول.
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DB_PATH = os.path.join(BASE_DIR, "database.db")

# على Render: حط متغير بيئة DB_PATH=/var/data/database.db (مسار الـ Persistent Disk)
# أول تشغيل بينسخ قاعدة البيانات اللي بالمشروع للديسك، وبعدين بتضل البيانات محفوظة
DB_PATH = os.environ.get("DB_PATH", SEED_DB_PATH)
if DB_PATH != SEED_DB_PATH and not os.path.exists(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(SEED_DB_PATH):
        import shutil
        shutil.copy2(SEED_DB_PATH, DB_PATH)

# بيانات الأدمن الافتراضي، بتنزرع بقاعدة البيانات أول مرة بس
# (لو ما كان في مستخدمين أصلاً). بعدين تقدر تغيري كلمة السر
# أو تضيف مستخدمين جداد من لوحة التحكم نفسها.
DEFAULT_ADMIN_USERNAME = "haidara"
DEFAULT_ADMIN_PASSWORD = "AlZeer2026"


# =========================================================
# SQLite
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        property_type TEXT DEFAULT 'شقة',
        listing_type TEXT DEFAULT 'sale',
        region TEXT DEFAULT 'دمشق',
        price REAL NOT NULL,
        area_sqm INTEGER DEFAULT 0,
        bedrooms INTEGER DEFAULT 0,
        bathrooms INTEGER DEFAULT 0,
        finish_quality TEXT DEFAULT 'ديلوكس',
        status TEXT DEFAULT 'available',
        image TEXT DEFAULT '',
        video TEXT DEFAULT '',
        description TEXT DEFAULT '',
        images TEXT DEFAULT '[]',
        featured INTEGER DEFAULT 0,
        featured_at TEXT DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()

    # أول تشغيل: لو ما في ولا مستخدم، منزرع الأدمن الافتراضي
    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD))
        )
        conn.commit()

    conn.close()


def add_activity(conn, message):
    conn.execute(
        "INSERT INTO activities (message) VALUES (?)",
        (message,)
    )


# =========================================================
# Migration: بنضيف أعمدة جديدة لجدول properties لو ناقصة
# (هيك ما منخسر البيانات القديمة الموجودة بقاعدة البيانات)
# =========================================================

PROPERTY_NEW_COLUMNS = {
    "video": "TEXT DEFAULT ''",
    "bedrooms": "INTEGER DEFAULT 0",
    "bathrooms": "INTEGER DEFAULT 0",
    "map_url": "TEXT DEFAULT ''",
    "latitude": "REAL DEFAULT NULL",
    "longitude": "REAL DEFAULT NULL",
}


def migrate_db():
    conn = get_db()

    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(properties)")
    }

    for column, definition in PROPERTY_NEW_COLUMNS.items():
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE properties ADD COLUMN {column} {definition}"
            )

    conn.commit()
    conn.close()


PROPERTY_TYPES = ("شقة", "فيلا", "محل تجاري", "مكتب", "أرض", "مستودع")
LISTING_TYPES = ("sale", "rent")
LISTING_TYPE_LABELS = {"sale": "بيع", "rent": "إيجار"}
REGIONS = (
    "دمشق", "ريف دمشق", "حلب", "حمص", "حماة", "اللاذقية",
    "طرطوس", "إدلب", "درعا", "السويداء", "القنيطرة",
    "دير الزور", "الرقة", "الحسكة"
)
FINISH_LEVELS = ("سوبر ديلوكس", "ديلوكس", "ممتاز", "عادي", "على الطوب")

MAX_FEATURED_PROPERTIES = 6



# =========================================================
# رابط الموقع على غوغل مابس
# =========================================================
# الأدمن بيلصق رابط المشاركة (Share) من غوغل مابس. الرابط القصير
# (maps.app.goo.gl) ما بينفع ينحط بـ iframe، فمنفتحو من السيرفر
# ومنسحب منه الإحداثيات (lat/lng) ومنخزنها مع الرابط الأصلي.

_GOOGLE_HOST_RE = re.compile(r"(^|\.)(google\.[a-z.]{2,6}|goo\.gl|g\.co)$", re.I)
_NUM = r"(-?\d{1,3}(?:\.\d+)?)"
_COORD_PATTERNS = (
    re.compile(r"!3d" + _NUM + r"!4d" + _NUM),                       # دبوس المكان بالضبط
    re.compile(r"@" + _NUM + r"," + _NUM),                           # مركز الخريطة
    re.compile(r"[?&](?:q|query|ll|destination|center)=" + _NUM + r",\s*" + _NUM),
)
_PLAIN_COORDS_RE = re.compile(r"^\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*$")


def _is_google_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(
        _GOOGLE_HOST_RE.search(parsed.hostname or "")
    )


def _valid_coords(lat, lng):
    return -90 <= lat <= 90 and -180 <= lng <= 180


def extract_coords(url):
    text = urllib.parse.unquote(url)

    # صفحة الموافقة (consent) الأوروبية بتحط الرابط الحقيقي بالباراميتر continue
    query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if "continue" in query:
        text = urllib.parse.unquote(query["continue"][0])

    for pattern in _COORD_PATTERNS:
        match = pattern.search(text)
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            if _valid_coords(lat, lng):
                return lat, lng
    return None, None


class _GoogleOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """بيمنع السيرفر يتبع تحويلة لأي موقع غير غوغل (حماية من SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_google_url(newurl):
            raise urllib.error.URLError("redirect to a non-google host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _follow_short_link(url):
    opener = urllib.request.build_opener(_GoogleOnlyRedirects)
    request_obj = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; AlzeerRealEstate/1.0)"}
    )
    with opener.open(request_obj, timeout=6) as response:
        return response.geturl()


def resolve_map_link(raw):
    """
    بترجع (map_url, latitude, longitude, warning).
    - فاضي: بيمسح الموقع.
    - إحداثيات مكتوبة ("33.51, 36.29"): بتنقبل مباشرة.
    - رابط غوغل: بنسحب منه الإحداثيات، ولو قصير بنفتحو من السيرفر.
    - أي شي تاني: ValueError.
    """
    raw = (raw or "").strip()

    if not raw:
        return "", None, None, None

    plain = _PLAIN_COORDS_RE.match(raw)
    if plain:
        lat, lng = float(plain.group(1)), float(plain.group(2))
        if not _valid_coords(lat, lng):
            raise ValueError("الإحداثيات خارج النطاق المسموح")
        url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
        return url, lat, lng, None

    if len(raw) > 2000 or not _is_google_url(raw):
        raise ValueError(
            "رابط الموقع لازم يكون رابط مشاركة من غوغل مابس "
            "(مثل https://maps.app.goo.gl/...)"
        )

    lat, lng = extract_coords(raw)
    if lat is not None:
        return raw, lat, lng, None

    try:
        final_url = _follow_short_link(raw)
        lat, lng = extract_coords(final_url)
        if lat is not None:
            return raw, lat, lng, None
    except (urllib.error.URLError, OSError, ValueError):
        pass

    return raw, None, None, (
        "انحفظ الرابط، بس ما قدرنا نسحب الإحداثيات منه. "
        "الزر رح يشتغل، لكن الخريطة المصغرة رح تعرض المنطقة العامة."
    )


def property_to_dict(row):
    """
    بنحول صف العقار من SQLite لقاموس بايثون عادي،
    وبنفك تشفير JSON لمصفوفة الصور، وبنجهز اسم listing_type_label
    حتى يبقى جاهز للعرض بالفرونت إند مباشرة.
    """
    prop = dict(row)

    try:
        prop["images"] = json.loads(prop.get("images") or "[]")
    except (TypeError, ValueError):
        prop["images"] = []

    prop["featured"] = bool(prop.get("featured"))
    prop["listing_type_label"] = LISTING_TYPE_LABELS.get(
        prop.get("listing_type"), "بيع"
    )

    return prop


# =========================================================
# حماية الدخول (Auth)
# =========================================================

def _current_user_still_valid():
    """بيتأكد إنه الـ user_id بالجلسة لسا موجود فعليًا بقاعدة البيانات
    (يعني لو حدا حذف حساب هالمستخدم، جلسته القديمة بتنفصل فورًا
    ولا تضل شغالة لحد ما تنتهي مدتها)."""
    user_id = session.get("user_id")
    if user_id is None:
        return False
    conn = get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return user is not None


def login_required_page(f):
    """يحمي صفحات HTML: لو مو مسجل دخول (أو حسابه انحذف)، بيرجعه عصفحة تسجيل الدخول."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _current_user_still_valid():
            session.clear()
            return redirect("/Login.html")
        return f(*args, **kwargs)
    return wrapper


def login_required_api(f):
    """يحمي الـ API: لو مو مسجل دخول (أو حسابه انحذف)، بيرجع 401 بدل تنفيذ الطلب."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _current_user_still_valid():
            session.clear()
            return jsonify({
                "success": False,
                "error": "لازم تسجلي دخول الأول"
            }), 401
        return f(*args, **kwargs)
    return wrapper


@app.post("/api/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()

    if user and check_password_hash(user["password_hash"], password):
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        return jsonify({"success": True, "username": user["username"]})

    return jsonify({
        "success": False,
        "error": "اسم المستخدم أو كلمة السر غلط"
    }), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"success": True})


@app.get("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False}), 401
    return jsonify({"logged_in": True, "username": session.get("username")})


# --------- إدارة المستخدمين (مين فيه يفوت عاللوحة) ---------

@app.get("/api/users")
@login_required_api
def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, created_at FROM users ORDER BY created_at"
    ).fetchall()
    conn.close()
    return jsonify({"success": True, "users": [dict(r) for r in rows]})


@app.post("/api/users")
@login_required_api
def add_user():
    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not username or len(password) < 6:
        return jsonify({
            "success": False,
            "error": "لازم اسم مستخدم وكلمة سر 6 أحرف عالأقل"
        }), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password))
        )
        add_activity(conn, f"تمت إضافة مستخدم جديد للوحة التحكم: {username}")
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({
            "success": False,
            "error": "في مستخدم فيه هيدا الاسم أصلاً"
        }), 400
    conn.close()
    return jsonify({"success": True})


@app.delete("/api/users/<int:user_id>")
@login_required_api
def delete_user(user_id):
    if session.get("user_id") == user_id:
        return jsonify({
            "success": False,
            "error": "ما فيك تحذفي حسابك انتي وانتي مسجلة دخول فيه"
        }), 400

    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# =========================================================
# HTML pages
# =========================================================

@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/index.html")
def index_page():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/Real-State.html")
def properties_page():
    return send_from_directory(BASE_DIR, "Real-State.html")


@app.route("/Dashboard.html")
@login_required_page
def dashboard_page():
    return send_from_directory(BASE_DIR, "Dashboard.html")


@app.route("/Login.html")
def login_page():
    return send_from_directory(BASE_DIR, "Login.html")


@app.route("/AboutUs.html")
def about_page():
    return send_from_directory(BASE_DIR, "AboutUs.html")


# يسمح فقط بتحميل ملفات المجلدات العامة (css / js / images)
# أي مسار ثاني (app.py، database.db، venv...) بيرجع 404
PUBLIC_DIRS = {"css", "js", "images"}


@app.route("/<path:filename>")
def static_files(filename):
    if filename.split("/")[0] not in PUBLIC_DIRS:
        abort(404)
    return send_from_directory(BASE_DIR, filename)


# =========================================================
# Dashboard API
# =========================================================

@app.get("/api/stats")
@login_required_api
def stats():
    conn = get_db()

    properties_count = conn.execute(
        "SELECT COUNT(*) FROM properties"
    ).fetchone()[0]

    for_sale_count = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE listing_type = 'sale'"
    ).fetchone()[0]

    for_rent_count = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE listing_type = 'rent'"
    ).fetchone()[0]

    featured_count = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE featured = 1"
    ).fetchone()[0]

    conn.close()

    return jsonify({
        "success": True,
        "properties": properties_count,
        "for_sale": for_sale_count,
        "for_rent": for_rent_count,
        "featured_count": featured_count
    })


@app.get("/api/activities")
@login_required_api
def activities():
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM activities
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "activities": [dict(row) for row in rows]
    })


# =========================================================
# Properties API
# =========================================================

@app.get("/api/properties")
def get_properties():
    search = request.args.get("search", "").strip()
    featured_only = request.args.get("featured", "").strip() == "1"

    conn = get_db()

    if featured_only:
        rows = conn.execute("""
            SELECT *
            FROM properties
            WHERE featured = 1
            ORDER BY featured_at DESC, id DESC
        """).fetchall()
    elif search:
        rows = conn.execute("""
            SELECT *
            FROM properties
            WHERE title LIKE ? OR region LIKE ?
            ORDER BY id DESC
        """, (f"%{search}%", f"%{search}%")).fetchall()
    else:
        rows = conn.execute("""
            SELECT *
            FROM properties
            ORDER BY id DESC
        """).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "properties": [property_to_dict(row) for row in rows]
    })


@app.get("/api/properties/<int:property_id>")
def get_property(property_id):
    conn = get_db()

    prop = conn.execute(
        "SELECT * FROM properties WHERE id = ?",
        (property_id,)
    ).fetchone()

    conn.close()

    if not prop:
        return jsonify({
            "success": False,
            "error": "العقار غير موجود"
        }), 404

    return jsonify({
        "success": True,
        "property": property_to_dict(prop)
    })


@app.post("/api/properties")
@login_required_api
def add_property():
    data = request.get_json() or {}

    title = str(data.get("title", "")).strip()
    price = data.get("price")
    area_sqm = data.get("area_sqm", 0)
    bedrooms = data.get("bedrooms", 0)
    bathrooms = data.get("bathrooms", 0)
    status = data.get("status", "available")

    property_type = data.get("property_type", "شقة")
    listing_type = data.get("listing_type", "sale")
    region = data.get("region", "دمشق")
    finish_quality = data.get("finish_quality", "ديلوكس")
    description = str(data.get("description", "")).strip()
    video = str(data.get("video", "")).strip()

    images_raw = data.get("images", [])
    images = (
        [str(x).strip() for x in images_raw if str(x).strip()]
        if isinstance(images_raw, list) else []
    )[:6]

    image = images[0] if images else str(data.get("image", "")).strip()

    featured = bool(data.get("featured", False))

    if not title or price is None:
        return jsonify({
            "success": False,
            "error": "اسم العقار والسعر مطلوبين"
        }), 400

    if status not in ("available", "reserved", "sold"):
        return jsonify({
            "success": False,
            "error": "حالة العقار غير صحيحة"
        }), 400

    if property_type not in PROPERTY_TYPES:
        return jsonify({
            "success": False,
            "error": "نوع العقار غير صحيح"
        }), 400

    if listing_type not in LISTING_TYPES:
        return jsonify({
            "success": False,
            "error": "نوع العرض (بيع/إيجار) غير صحيح"
        }), 400

    if region not in REGIONS:
        return jsonify({
            "success": False,
            "error": "المنطقة غير صحيحة"
        }), 400

    if finish_quality not in FINISH_LEVELS:
        return jsonify({
            "success": False,
            "error": "حالة الإكساء غير صحيحة"
        }), 400

    try:
        price = float(price)
        area_sqm = int(area_sqm or 0)
        bedrooms = int(bedrooms or 0)
        bathrooms = int(bathrooms or 0)
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "error": "تأكد من القيم الرقمية"
        }), 400

    try:
        map_url, latitude, longitude, map_warning = resolve_map_link(
            data.get("map_url", "")
        )
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400

    conn = get_db()

    if featured:
        current_featured = conn.execute(
            "SELECT COUNT(*) FROM properties WHERE featured = 1"
        ).fetchone()[0]

        if current_featured >= MAX_FEATURED_PROPERTIES:
            conn.close()

            return jsonify({
                "success": False,
                "error": (
                    f"في {MAX_FEATURED_PROPERTIES} عقارات مميزة مسبقاً، "
                    "شيل وحدة الأول قبل ما تضيف جديدة"
                )
            }), 400

    featured_at = datetime.now().isoformat() if featured else None

    cursor = conn.execute("""
        INSERT INTO properties (
            title,
            property_type,
            listing_type,
            region,
            price,
            area_sqm,
            bedrooms,
            bathrooms,
            finish_quality,
            status,
            image,
            video,
            description,
            images,
            featured,
            featured_at,
            map_url,
            latitude,
            longitude
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        title,
        property_type,
        listing_type,
        region,
        price,
        area_sqm,
        bedrooms,
        bathrooms,
        finish_quality,
        status,
        image,
        video,
        description,
        json.dumps(images, ensure_ascii=False),
        1 if featured else 0,
        featured_at,
        map_url,
        latitude,
        longitude
    ))

    property_id = cursor.lastrowid

    add_activity(
        conn,
        f"تمت إضافة عقار جديد: {title}"
    )

    conn.commit()

    prop = conn.execute(
        "SELECT * FROM properties WHERE id = ?",
        (property_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "success": True,
        "property": property_to_dict(prop),
        "map_warning": map_warning
    })


@app.put("/api/properties/<int:property_id>")
@login_required_api
def edit_property(property_id):
    data = request.get_json() or {}

    conn = get_db()

    old_property = conn.execute(
        "SELECT * FROM properties WHERE id = ?",
        (property_id,)
    ).fetchone()

    if not old_property:
        conn.close()

        return jsonify({
            "success": False,
            "error": "العقار غير موجود"
        }), 404

    old_property = property_to_dict(old_property)

    title = data.get("title", old_property["title"])
    price = data.get("price", old_property["price"])
    area_sqm = data.get("area_sqm", old_property["area_sqm"])
    bedrooms = data.get("bedrooms", old_property["bedrooms"])
    bathrooms = data.get("bathrooms", old_property["bathrooms"])
    status = data.get("status", old_property["status"])

    property_type = data.get("property_type", old_property["property_type"])
    listing_type = data.get("listing_type", old_property["listing_type"])
    region = data.get("region", old_property["region"])
    finish_quality = data.get("finish_quality", old_property["finish_quality"])
    description = data.get("description", old_property["description"])
    video = data.get("video", old_property["video"])

    if "images" in data and isinstance(data["images"], list):
        images = [
            str(x).strip() for x in data["images"] if str(x).strip()
        ][:6]
    else:
        images = old_property["images"]

    image = images[0] if images else data.get("image", old_property["image"])

    if property_type not in PROPERTY_TYPES:
        conn.close()
        return jsonify({"success": False, "error": "نوع العقار غير صحيح"}), 400

    if listing_type not in LISTING_TYPES:
        conn.close()
        return jsonify({"success": False, "error": "نوع العرض غير صحيح"}), 400

    if region not in REGIONS:
        conn.close()
        return jsonify({"success": False, "error": "المنطقة غير صحيحة"}), 400

    if finish_quality not in FINISH_LEVELS:
        conn.close()
        return jsonify({"success": False, "error": "حالة الإكساء غير صحيحة"}), 400

    if status not in ("available", "reserved", "sold"):
        conn.close()
        return jsonify({"success": False, "error": "حالة العقار غير صحيحة"}), 400

    try:
        price = float(price)
        area_sqm = int(area_sqm or 0)
        bedrooms = int(bedrooms or 0)
        bathrooms = int(bathrooms or 0)
    except (ValueError, TypeError):
        conn.close()
        return jsonify({"success": False, "error": "تأكد من القيم الرقمية"}), 400

    # موقع غوغل مابس: منعيد سحب الإحداثيات بس لما الرابط يتغير
    # (أو لما كان محفوظ بدون إحداثيات). تبديل "مميز" مثلاً ما بيلمس الموقع.
    old_map_url = old_property.get("map_url") or ""
    new_map_raw = str(data.get("map_url", old_map_url)).strip()
    map_warning = None

    if new_map_raw == old_map_url and (
        not old_map_url or old_property.get("latitude") is not None
    ):
        map_url = old_map_url
        latitude = old_property.get("latitude")
        longitude = old_property.get("longitude")
    else:
        try:
            map_url, latitude, longitude, map_warning = resolve_map_link(
                new_map_raw
            )
        except ValueError as error:
            conn.close()
            return jsonify({"success": False, "error": str(error)}), 400

    featured = bool(data.get("featured", old_property["featured"]))
    featured_at = old_property["featured_at"]

    if featured and not old_property["featured"]:
        current_featured = conn.execute(
            "SELECT COUNT(*) FROM properties WHERE featured = 1"
        ).fetchone()[0]

        if current_featured >= MAX_FEATURED_PROPERTIES:
            conn.close()
            return jsonify({
                "success": False,
                "error": (
                    f"في {MAX_FEATURED_PROPERTIES} عقارات مميزة مسبقاً، "
                    "شيل وحدة الأول قبل ما تضيف جديدة"
                )
            }), 400

        featured_at = datetime.now().isoformat()
    elif not featured:
        featured_at = None

    conn.execute("""
        UPDATE properties
        SET title = ?,
            property_type = ?,
            listing_type = ?,
            region = ?,
            price = ?,
            area_sqm = ?,
            bedrooms = ?,
            bathrooms = ?,
            finish_quality = ?,
            status = ?,
            image = ?,
            video = ?,
            description = ?,
            images = ?,
            featured = ?,
            featured_at = ?,
            map_url = ?,
            latitude = ?,
            longitude = ?
        WHERE id = ?
    """, (
        title,
        property_type,
        listing_type,
        region,
        price,
        area_sqm,
        bedrooms,
        bathrooms,
        finish_quality,
        status,
        image,
        video,
        description,
        json.dumps(images, ensure_ascii=False),
        1 if featured else 0,
        featured_at,
        map_url,
        latitude,
        longitude,
        property_id
    ))

    add_activity(conn, f"تم تعديل بيانات العقار: {title}")

    conn.commit()

    prop = conn.execute(
        "SELECT * FROM properties WHERE id = ?",
        (property_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "success": True,
        "property": property_to_dict(prop),
        "map_warning": map_warning
    })


@app.delete("/api/properties/<int:property_id>")
@login_required_api
def delete_property(property_id):
    conn = get_db()

    prop = conn.execute(
        "SELECT * FROM properties WHERE id = ?",
        (property_id,)
    ).fetchone()

    if not prop:
        conn.close()
        return jsonify({"success": False, "error": "العقار غير موجود"}), 404

    conn.execute("DELETE FROM properties WHERE id = ?", (property_id,))
    add_activity(conn, f"تم حذف العقار: {prop['title']}")
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# =========================================================
# تشغيل السيرفر
# =========================================================

init_db()
migrate_db()

if __name__ == "__main__":
    app.run(debug=True)
