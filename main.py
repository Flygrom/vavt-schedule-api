import io
import os
import re
import requests
import pdfplumber
import cloudinary
import cloudinary.uploader
from bs4 import BeautifulSoup
from datetime import datetime
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, inspect, text, or_
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# MARK: - Cloudinary
# Секреты берутся из переменных окружения Railway (Settings -> Variables),
# никогда не хардкодим их в коде.

cloudinary.config(
    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"],
)

# MARK: - DeepL (перевод названий предметов на английский)
# Опционально: если ключ не задан, английский режим просто отдаёт русский текст как есть.

DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY")
DEEPL_URL = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY and DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)


def translate_texts(texts: list, target_lang: str = "EN-US") -> list:
    if not DEEPL_API_KEY or not texts:
        return texts
    try:
        resp = requests.post(
            DEEPL_URL,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data={"text": texts, "target_lang": target_lang, "source_lang": "RU"},
            timeout=15,
        )
        resp.raise_for_status()
        return [t["text"] for t in resp.json()["translations"]]
    except Exception:
        return texts

# MARK: - База данных

# На Railway контейнер пересоздаётся при каждом деплое — если файл базы лежит
# просто в рабочей директории, вся база стирается при каждом git push. Нужен
# постоянный том (Volume), примонтированный в /data, и DATABASE_URL,
# указывающий внутрь него (см. README/инструкцию по деплою).
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./homework.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Homework(Base):
    __tablename__ = "homework"

    id = Column(Integer, primary_key=True, index=True)
    group_name = Column(String, index=True)
    subject = Column(String)
    author_name = Column(String)
    text = Column(String)
    is_shared = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_hidden = Column(Boolean, default=False)
    owner_token = Column(String, index=True, nullable=True)

    files = relationship("HomeworkFile", back_populates="homework", cascade="all, delete-orphan")


class HomeworkFile(Base):
    __tablename__ = "homework_files"

    id = Column(Integer, primary_key=True, index=True)
    homework_id = Column(Integer, ForeignKey("homework.id"))
    file_url = Column(String)
    file_type = Column(String)
    file_name = Column(String)

    homework = relationship("Homework", back_populates="files")


class TeacherLesson(Base):
    __tablename__ = "teacher_lessons"

    id = Column(Integer, primary_key=True, index=True)
    faculty = Column(String, index=True)
    teacher_name = Column(String, index=True)
    day = Column(String)
    time_start = Column(String)
    time_end = Column(String)
    subject = Column(String)
    room = Column(String)
    group_name = Column(String)
    week_label = Column(String)
    indexed_at = Column(DateTime, default=datetime.utcnow)


class Lesson(Base):
    """Проиндексированные пары по каждой группе — заполняется /index-all и лениво
    при первом запросе /schedule-db, чтобы приложение не парсило PDF на каждый чих."""
    __tablename__ = "lessons"

    id = Column(Integer, primary_key=True, index=True)
    pdf_url = Column(String, index=True)
    group_name = Column(String, index=True)
    faculty = Column(String, nullable=True)
    week_label = Column(String, nullable=True)
    day = Column(String)
    time_start = Column(String)
    time_end = Column(String)
    subject = Column(String)
    teacher = Column(String, nullable=True)
    room = Column(String, nullable=True)
    lesson_type = Column(String, nullable=True)
    indexed_at = Column(DateTime, default=datetime.utcnow)


class SubjectTranslation(Base):
    """Кэш переводов названий предметов — переводим каждое название один раз,
    а не при каждом запросе (DeepL просят так же дедуплицировать самостоятельно)."""
    __tablename__ = "subject_translations"

    id = Column(Integer, primary_key=True, index=True)
    subject_ru = Column(String, unique=True, index=True)
    subject_en = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow)


class HomeworkReport(Base):
    __tablename__ = "homework_reports"

    id = Column(Integer, primary_key=True, index=True)
    homework_id = Column(Integer, ForeignKey("homework.id"))
    reporter_name = Column(String)
    reason = Column(String)
    device_token = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BlockedUser(Base):
    __tablename__ = "blocked_users"

    id = Column(Integer, primary_key=True, index=True)
    blocker_name = Column(String, index=True, nullable=True)
    blocked_name = Column(String, index=True)
    device_token = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def _ensure_column(table: str, column: str, coltype: str):
    """SQLite не добавляет новые колонки через create_all — доращиваем схему вручную."""
    inspector = inspect(engine)
    existing = [c["name"] for c in inspector.get_columns(table)]
    if column not in existing:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


_ensure_column("homework", "owner_token", "VARCHAR")
_ensure_column("homework_reports", "device_token", "VARCHAR")
_ensure_column("blocked_users", "device_token", "VARCHAR")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

TYPE_MAP = {
    "с": "Семинар", "л": "Лекция", "пр": "Практика", "пз": "Практика",
    "з": "Зачёт", "зач": "Зачёт", "зачет": "Зачёт", "зачёт": "Зачёт",
    "экз": "Экзамен"
}


# MARK: - PDF-парсинг

def find_group_col(table, group_filter):
    for row in table:
        for col_idx, cell in enumerate(row):
            if cell and group_filter == str(cell).strip():
                return col_idx
    return None


CID_PATTERN = re.compile(r"\(cid:\d+\)")


def clean_pdf_text(value):
    """pdfplumber иногда не может сопоставить глиф шрифта с юникодом и
    возвращает сырое обозначение вида '(cid:10)' — вырезаем такой мусор."""
    if value is None:
        return value
    cleaned = CID_PATTERN.sub("", str(value))
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned if cleaned else None


def get_pdf_table(pdf_url: str):
    r = requests.get(pdf_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                return [[clean_pdf_text(cell) for cell in row] for row in tables[0]]
    return None


def extract_day(cell_text: str):
    if not cell_text:
        return None
    first_line = str(cell_text).strip().split("\n")[0].strip()
    return first_line if first_line in DAYS else None


def find_first_day_after(table, start_row: int):
    for row in table[start_row:]:
        if not row:
            continue
        day = extract_day(str(row[0] or ""))
        if day:
            return day
    return None


def find_first_group_col(table):
    pattern = re.compile(r"[А-ЯЁ]\d{2}[А-ЯЁ][–\-—][А-ЯЁ]{2,6}\.\d")
    for row in table:
        for col_idx, cell in enumerate(row):
            if cell and pattern.match(str(cell).strip()):
                return col_idx
    return 2


def normalize_teacher_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def resolve_colspan_value(row, col_index: int, first_group_col: int):
    if col_index >= len(row):
        return None
    direct_value = row[col_index]
    if direct_value is None:
        for i in range(col_index - 1, first_group_col - 1, -1):
            if i >= len(row):
                continue
            candidate = row[i]
            if candidate == "":
                return None
            if candidate and str(candidate).strip():
                return str(candidate)
        return None
    if direct_value and str(direct_value).strip():
        return str(direct_value)
    return None


def parse_lesson_cell(text: str):
    text = (text or "").strip()
    if not text or len(text) < 3:
        return None

    original = text

    room_match = re.search(r"[Аа]уд\.?\s*([\d\.]+[а-яА-Яa-zA-Z]?)", text)
    room = room_match.group(1) if room_match else None
    if room_match:
        text = text[:room_match.start()] + text[room_match.end():]

    type_pattern = re.compile(r"\((с|л|пр|пз|з|зач|зачет|зачёт|экз|С|Л|ПР|ПЗ|З|ЗАЧ|ЗАЧЕТ|ЗАЧЁТ|ЭКЗ)\.?\)")
    matches = list(type_pattern.finditer(text))
    lesson_type = None
    if matches:
        last_match = matches[-1]
        type_raw = last_match.group(1).lower().replace(".", "")
        lesson_type = TYPE_MAP.get(type_raw, type_raw.upper())
        text = text[:last_match.start()] + text[last_match.end():]

    teacher_match = re.search(r"[А-ЯЁ][а-яё]+\s*\n?\s*[А-ЯЁ]\.\s?[А-ЯЁ]\.", text)
    teacher = normalize_teacher_name(teacher_match.group(0)) if teacher_match else None
    if teacher_match:
        text = text[:teacher_match.start()] + text[teacher_match.end():]

    subject = re.sub(r"\s+", " ", text).strip(" \n.,;:-_")
    if not subject:
        subject = original.strip()

    return {"subject": subject, "teacher": teacher, "room": room, "type": lesson_type}


def parse_pdf(pdf_url: str, group_filter: str = None, prefetched_table=None):
    table = prefetched_table if prefetched_table is not None else get_pdf_table(pdf_url)
    if not table:
        return []

    lessons = []
    first_group_col = find_first_group_col(table)

    if group_filter:
        group_col = find_group_col(table, group_filter)
        if group_col is None:
            return []
        target_cols = [group_col]
    else:
        max_cols = max(len(row) for row in table)
        target_cols = list(range(first_group_col, max_cols))

    current_day = None
    current_time = None
    seen = set()

    for row_idx, row in enumerate(table):
        if not row:
            continue

        first_cell = str(row[0] or "").strip()
        second_cell = str(row[1] or "").strip() if len(row) > 1 else ""

        day = extract_day(first_cell)
        if day:
            current_day = day

        time_source = second_cell if second_cell else first_cell
        time_match = re.search(r"(\d{1,2}[.:]\d{2})\s*[-–—]\s*(\d{1,2}[.:]\d{2})", time_source)
        if time_match:
            current_time = (
                time_match.group(1).replace(".", ":"),
                time_match.group(2).replace(".", ":")
            )

        if not current_time:
            continue

        effective_day = current_day or find_first_day_after(table, row_idx)

        def add_lesson(cell_text):
            parsed = parse_lesson_cell(cell_text)
            if not parsed:
                return
            key = (effective_day, current_time[0], parsed["subject"][:30])
            if key in seen:
                return
            seen.add(key)
            lessons.append({
                "day": effective_day,
                "timeStart": current_time[0],
                "timeEnd": current_time[1],
                **parsed
            })

        for col in target_cols:
            cell_text = resolve_colspan_value(row, col, first_group_col)
            if not cell_text:
                continue
            add_lesson(cell_text)

    return lessons


def get_translated_subjects(db: Session, subjects_ru: list) -> dict:
    """Переводит уникальные названия предметов, кэшируя результат в БД —
    один и тот же предмет переводится через DeepL только один раз за всё время."""
    unique = list(dict.fromkeys(s for s in subjects_ru if s))
    if not unique:
        return {}

    existing = db.query(SubjectTranslation).filter(SubjectTranslation.subject_ru.in_(unique)).all()
    result = {e.subject_ru: e.subject_en for e in existing}

    missing = [s for s in unique if s not in result]
    if missing:
        translated = translate_texts(missing)
        for ru, en in zip(missing, translated):
            db.add(SubjectTranslation(subject_ru=ru, subject_en=en))
            result[ru] = en
        db.commit()

    return result


@app.get("/")
def root():
    return {"status": "ok", "service": "ВАВТ Расписание API"}


@app.get("/schedule")
def get_schedule(pdf_url: str, group: str = None):
    try:
        lessons = parse_pdf(pdf_url, group)
        return {"ok": True, "group": group, "count": len(lessons), "lessons": lessons}
    except Exception as e:
        return {"ok": False, "error": str(e), "lessons": []}


@app.get("/schedule-db")
def get_schedule_db(pdf_url: str, group: str = None, lang: str = "ru"):
    """Тот же формат ответа, что /schedule, но сначала читает уже проиндексированные
    пары из базы — и только если для этого pdf_url+группы вообще ничего нет,
    парсит PDF один раз и сохраняет результат на будущее (для всех пользователей).
    lang=en — подставляет переведённые названия предметов (см. get_translated_subjects)."""
    db: Session = SessionLocal()
    try:
        query = db.query(Lesson).filter(Lesson.pdf_url == pdf_url)
        if group:
            query = query.filter(Lesson.group_name == group)
        entries = query.all()

        if entries:
            lessons = [{
                "day": e.day,
                "timeStart": e.time_start,
                "timeEnd": e.time_end,
                "subject": e.subject,
                "teacher": e.teacher,
                "room": e.room,
                "type": e.lesson_type,
            } for e in entries]
        else:
            # Ничего не найдено — это первый запрос по этой неделе/группе, парсим и кешируем
            lessons = parse_pdf(pdf_url, group)
            for lesson in lessons:
                db.add(Lesson(
                    pdf_url=pdf_url,
                    group_name=group,
                    day=lesson.get("day"),
                    time_start=lesson.get("timeStart"),
                    time_end=lesson.get("timeEnd"),
                    subject=lesson.get("subject"),
                    teacher=lesson.get("teacher"),
                    room=lesson.get("room"),
                    lesson_type=lesson.get("type"),
                ))
            db.commit()

        if lang == "en":
            translations = get_translated_subjects(db, [l.get("subject") for l in lessons])
            for l in lessons:
                if l.get("subject") in translations:
                    l["subject"] = translations[l["subject"]]

        return {"ok": True, "group": group, "count": len(lessons), "lessons": lessons}
    except Exception as e:
        return {"ok": False, "error": str(e), "lessons": []}
    finally:
        db.close()


@app.get("/groups")
def get_groups(pdf_url: str):
    try:
        table = get_pdf_table(pdf_url)
        if not table:
            return {"ok": True, "groups": []}
        groups = []
        pattern = re.compile(r"^[А-ЯЁ]\d{2}[А-ЯЁ][–\-—][А-ЯЁ]{2,6}\.\d$")
        for row in table:
            for cell in row:
                if cell and pattern.match(str(cell).strip()):
                    name = str(cell).strip()
                    if name not in groups:
                        groups.append(name)
        return {"ok": True, "groups": groups}
    except Exception as e:
        return {"ok": False, "error": str(e), "groups": []}


# MARK: - Домашние задания

class HomeworkCreate(BaseModel):
    group_name: str
    subject: str
    author_name: str
    text: str
    is_shared: bool = True
    device_token: str


def homework_to_dict(entry: Homework):
    return {
        "id": entry.id,
        "group_name": entry.group_name,
        "subject": entry.subject,
        "author_name": entry.author_name,
        "text": entry.text,
        "is_shared": entry.is_shared,
        "created_at": entry.created_at.isoformat(),
        "files": [
            {"id": f.id, "file_url": f.file_url, "file_type": f.file_type, "file_name": f.file_name}
            for f in entry.files
        ]
    }


@app.post("/homework")
def create_homework(hw: HomeworkCreate):
    db: Session = SessionLocal()
    try:
        entry = Homework(
            group_name=hw.group_name,
            subject=hw.subject,
            author_name=hw.author_name,
            text=hw.text,
            is_shared=hw.is_shared,
            owner_token=hw.device_token,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return {"ok": True, **homework_to_dict(entry)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@app.get("/homework")
def list_homework(group_name: str, author_name: str = None, device_token: str = None):
    db: Session = SessionLocal()
    try:
        # Список тех, кого текущий пользователь заблокировал (по device_token,
        # т.к. author_name — это произвольное имя и им нельзя удостоверять личность)
        blocked_names = []
        if device_token:
            blocked_rows = db.query(BlockedUser).filter(BlockedUser.device_token == device_token).all()
            blocked_names = [b.blocked_name for b in blocked_rows]

        query = db.query(Homework).filter(
            Homework.group_name == group_name,
            Homework.is_hidden == False
        )

        conditions = [Homework.is_shared == True]
        if device_token:
            conditions.append(Homework.owner_token == device_token)
        if author_name:
            conditions.append(Homework.author_name == author_name)

        entries = query.filter(or_(*conditions)).order_by(Homework.created_at.desc()).all()

        # Отфильтровываем ДЗ от заблокированных авторов
        if blocked_names:
            entries = [e for e in entries if e.author_name not in blocked_names]

        return {"ok": True, "items": [homework_to_dict(e) for e in entries]}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    finally:
        db.close()


@app.delete("/homework/{homework_id}")
def delete_homework(homework_id: int, device_token: str):
    db: Session = SessionLocal()
    try:
        entry = db.query(Homework).filter(Homework.id == homework_id).first()
        if not entry:
            return {"ok": False, "error": "Не найдено"}
        # owner_token — это непередаваемый секрет с устройства автора, в отличие
        # от author_name (который виден всем и раньше был единственной "проверкой").
        if not entry.owner_token or entry.owner_token != device_token:
            return {"ok": False, "error": "Можно удалять только своё ДЗ"}
        db.delete(entry)
        db.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@app.post("/homework/{homework_id}/files")
async def upload_homework_file(homework_id: int, file: UploadFile = File(...)):
    db: Session = SessionLocal()
    try:
        entry = db.query(Homework).filter(Homework.id == homework_id).first()
        if not entry:
            return {"ok": False, "error": "Домашнее задание не найдено"}

        content = await file.read()
        content_type = file.content_type or ""
        is_image = content_type.startswith("image/")

        upload_result = cloudinary.uploader.upload(
            content,
            resource_type="image" if is_image else "raw",
            folder="vavt_homework",
        )

        file_entry = HomeworkFile(
            homework_id=homework_id,
            file_url=upload_result["secure_url"],
            file_type="image" if is_image else "document",
            file_name=file.filename or "file",
        )
        db.add(file_entry)
        db.commit()
        db.refresh(file_entry)

        return {
            "ok": True, "id": file_entry.id, "file_url": file_entry.file_url,
            "file_type": file_entry.file_type, "file_name": file_entry.file_name,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# MARK: - Жалобы на ДЗ

class ReportCreate(BaseModel):
    homework_id: int
    reporter_name: str
    reason: str
    device_token: str


AUTO_HIDE_THRESHOLD = 3  # после скольких уникальных жалоб ДЗ скрывается автоматически


@app.post("/homework/report")
def report_homework(report: ReportCreate):
    db: Session = SessionLocal()
    try:
        entry = db.query(Homework).filter(Homework.id == report.homework_id).first()
        if not entry:
            return {"ok": False, "error": "Домашнее задание не найдено"}

        # Не даём одному устройству пожаловаться на одно и то же ДЗ дважды
        # (по device_token, а не по вводимому вручную имени, которое легко сменить)
        existing = db.query(HomeworkReport).filter(
            HomeworkReport.homework_id == report.homework_id,
            HomeworkReport.device_token == report.device_token
        ).first()
        if existing:
            return {"ok": True, "already_reported": True}

        new_report = HomeworkReport(
            homework_id=report.homework_id,
            reporter_name=report.reporter_name,
            reason=report.reason,
            device_token=report.device_token,
        )
        db.add(new_report)

        report_count = db.query(HomeworkReport).filter(
            HomeworkReport.homework_id == report.homework_id
        ).count() + 1

        if report_count >= AUTO_HIDE_THRESHOLD:
            entry.is_hidden = True

        db.commit()
        return {"ok": True, "hidden": entry.is_hidden}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# MARK: - Блокировка пользователей

class BlockUserRequest(BaseModel):
    blocked_name: str
    device_token: str


@app.post("/users/block")
def block_user(req: BlockUserRequest):
    db: Session = SessionLocal()
    try:
        existing = db.query(BlockedUser).filter(
            BlockedUser.device_token == req.device_token,
            BlockedUser.blocked_name == req.blocked_name
        ).first()
        if existing:
            return {"ok": True, "already_blocked": True}

        entry = BlockedUser(device_token=req.device_token, blocked_name=req.blocked_name)
        db.add(entry)
        db.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@app.post("/users/unblock")
def unblock_user(req: BlockUserRequest):
    db: Session = SessionLocal()
    try:
        entry = db.query(BlockedUser).filter(
            BlockedUser.device_token == req.device_token,
            BlockedUser.blocked_name == req.blocked_name
        ).first()
        if entry:
            db.delete(entry)
            db.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@app.get("/users/blocked")
def get_blocked_users(device_token: str):
    db: Session = SessionLocal()
    try:
        rows = db.query(BlockedUser).filter(BlockedUser.device_token == device_token).all()
        return {"ok": True, "blocked": [r.blocked_name for r in rows]}
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": []}
    finally:
        db.close()


# MARK: - Обход сайта для индексации преподавателей

def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


def parse_tile_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.select("a.link-item-a"):
        href = a.get("href", "")
        span = a.select_one("span")
        title = span.text.strip() if span else ""
        if href and title:
            links.append({"href": href, "title": title})
    return links


def is_schedule_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one("#_list_sched") is not None


def parse_schedule_page_pdfs(html: str):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select("#_list_sched ._items_"):
        title_el = item.select_one("h2.title-box")
        week_label = title_el.text.strip() if title_el else ""
        for a in item.select(".rasp-list a"):
            href = a.get("href", "")
            if href:
                full_url = href if href.startswith("http") else "https://www.vavt.ru" + href
                results.append((full_url, week_label))
    return results


def index_faculty_teachers(faculty_tile_href: str, faculty_name: str, max_weeks_per_group: int = 6):
    full_url = faculty_tile_href if faculty_tile_href.startswith("http") else "https://www.vavt.ru" + faculty_tile_href
    html = fetch_html(full_url)

    if not is_schedule_page(html):
        course_tiles = parse_tile_links(html)
        all_results = []
        for tile in course_tiles:
            all_results += index_faculty_teachers(tile["href"], faculty_name, max_weeks_per_group)
        return all_results

    # ВАЖНО: на одной странице курса может быть несколько профилей (несколько
    # PDF на каждую неделю). Раньше здесь резалось "первые N ссылок подряд",
    # из-за чего при 3+ профилях на неделю часть профилей/групп вообще не
    # попадала в индекс. Теперь берём первые N уникальных недель целиком,
    # со всеми профилями внутри них.
    all_pdfs = parse_schedule_page_pdfs(html)
    seen_weeks = []
    pdf_list = []
    for pdf_url, week_label in all_pdfs:
        if week_label not in seen_weeks:
            if len(seen_weeks) >= max_weeks_per_group:
                break
            seen_weeks.append(week_label)
        pdf_list.append((pdf_url, week_label))

    results = []
    for pdf_url, week_label in pdf_list:
        try:
            table = get_pdf_table(pdf_url)
            if not table:
                continue

            pattern = re.compile(r"^[А-ЯЁ]\d{2}[А-ЯЁ][–\-—][А-ЯЁ]{2,6}\.\d$")
            groups = []
            for row in table:
                for cell in row:
                    if cell and pattern.match(str(cell).strip()):
                        name = str(cell).strip()
                        if name not in groups:
                            groups.append(name)

            db: Session = SessionLocal()
            try:
                # Один pdf_url = одна неделя одного профиля — чистим только его,
                # чтобы не задеть данные других групп/недель при повторном прогоне.
                db.query(Lesson).filter(Lesson.pdf_url == pdf_url).delete()

                for group_name in groups:
                    group_lessons = parse_pdf(pdf_url, group_filter=group_name, prefetched_table=table)
                    for lesson in group_lessons:
                        db.add(Lesson(
                            pdf_url=pdf_url,
                            group_name=group_name,
                            faculty=faculty_name,
                            week_label=week_label,
                            day=lesson.get("day"),
                            time_start=lesson.get("timeStart"),
                            time_end=lesson.get("timeEnd"),
                            subject=lesson.get("subject"),
                            teacher=lesson.get("teacher"),
                            room=lesson.get("room"),
                            lesson_type=lesson.get("type"),
                        ))
                        if lesson.get("teacher"):
                            results.append({
                                "faculty": faculty_name,
                                "teacher_name": normalize_teacher_name(lesson["teacher"]),
                                "day": lesson.get("day"),
                                "time_start": lesson.get("timeStart"),
                                "time_end": lesson.get("timeEnd"),
                                "subject": lesson.get("subject"),
                                "room": lesson.get("room"),
                                "group_name": group_name,
                                "week_label": week_label,
                            })

                db.commit()
            finally:
                db.close()
        except Exception:
            continue

    return results


ROOT_SCHEDULE_URL = "https://www.vavt.ru/schedule/"


@app.post("/purge-lessons")
def purge_lessons():
    """Служебная ручка: чистит кэш пар (Lesson), например после фикса парсинга,
    чтобы старые "грязные" записи не продолжали отдаваться из базы."""
    db: Session = SessionLocal()
    try:
        deleted = db.query(Lesson).delete()
        db.commit()
        return {"ok": True, "deleted": deleted}
    finally:
        db.close()


@app.post("/index-all")
def trigger_full_index(max_weeks_per_group: int = 6):
    """Индексирует преподавателей ВСЕХ факультетов, а не одного вручную указанного."""
    try:
        html = fetch_html(ROOT_SCHEDULE_URL)
        faculties = parse_tile_links(html)

        total = 0
        summary = []
        for faculty in faculties:
            try:
                results = index_faculty_teachers(faculty["href"], faculty["title"], max_weeks_per_group)

                db: Session = SessionLocal()
                try:
                    db.query(TeacherLesson).filter(TeacherLesson.faculty == faculty["title"]).delete()
                    for r in results:
                        db.add(TeacherLesson(**r))
                    db.commit()
                finally:
                    db.close()

                total += len(results)
                summary.append({"faculty": faculty["title"], "indexed_count": len(results)})
            except Exception as e:
                summary.append({"faculty": faculty.get("title"), "error": str(e)})

        return {"ok": True, "total_indexed": total, "faculties": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/index-faculty")
def trigger_faculty_index(faculty_href: str, faculty_name: str):
    try:
        results = index_faculty_teachers(faculty_href, faculty_name)

        db: Session = SessionLocal()
        try:
            db.query(TeacherLesson).filter(TeacherLesson.faculty == faculty_name).delete()
            for r in results:
                entry = TeacherLesson(**r)
                db.add(entry)
            db.commit()
        finally:
            db.close()

        return {"ok": True, "indexed_count": len(results)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/teachers/search")
def search_teachers(query: str, faculty: str = None):
    db: Session = SessionLocal()
    try:
        q = db.query(TeacherLesson).filter(TeacherLesson.teacher_name.ilike(f"%{query}%"))
        if faculty:
            q = q.filter(TeacherLesson.faculty == faculty)

        entries = q.order_by(TeacherLesson.day, TeacherLesson.time_start).all()

        return {
            "ok": True,
            "items": [
                {
                    "teacher_name": e.teacher_name,
                    "day": e.day,
                    "time_start": e.time_start,
                    "time_end": e.time_end,
                    "subject": e.subject,
                    "room": e.room,
                    "group_name": e.group_name,
                    "week_label": e.week_label,
                }
                for e in entries
            ]
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    finally:
        db.close()


@app.get("/debug-table")
def debug_table(pdf_url: str):
    try:
        table = get_pdf_table(pdf_url)
        if not table:
            return {"ok": False, "error": "no table"}
        rows = []
        for i, row in enumerate(table):
            rows.append({"row": i, "cells": [str(c)[:80] if c else None for c in row]})
        return {"ok": True, "rows": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
