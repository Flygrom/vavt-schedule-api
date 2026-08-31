import io
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
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

# MARK: - Cloudinary

cloudinary.config(
    cloud_name="bfolh7o5",
    api_key="519197542141111",
    api_secret="-C76Y2uFwF4xkI5E0282rkuAjOg"
)

# MARK: - База данных

DATABASE_URL = "sqlite:///./homework.db"
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


Base.metadata.create_all(bind=engine)

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


def get_pdf_table(pdf_url: str):
    r = requests.get(pdf_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                return tables[0]
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
    """
    Возвращает "эффективное" значение ячейки в позиции col_index, учитывая
    объединённые ячейки (colspan): если сама ячейка пустая, ищем ближайшую
    непустую ячейку СЛЕВА в пределах диапазона групп — она может быть
    объединённой ячейкой, растянутой на col_index.

    ВАЖНО: если между найденной непустой ячейкой слева и col_index
    есть ДРУГАЯ непустая ячейка — значит col_index не покрывается
    той дальней ячейкой, и мы возвращаем None (пусто по-настоящему).
    """
    if col_index >= len(row):
        return None

    direct_value = row[col_index]
    if direct_value and str(direct_value).strip():
        return str(direct_value)

    # Ищем ближайшую непустую ячейку слева, в пределах групповых колонок
    for i in range(col_index - 1, first_group_col - 1, -1):
        if i >= len(row):
            continue
        candidate = row[i]
        if candidate and str(candidate).strip():
            return str(candidate)
        # Если встретили ещё одну явно пустую ячейку — продолжаем искать левее
        # (None и '' оба считаются "пустыми" в объединённой зоне)

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


def is_continuation_only(cell_text: str) -> bool:
    stripped = cell_text.strip()
    if not stripped:
        return False
    teacher_pattern = r"[А-ЯЁ][а-яё]+\s*\n?\s*[А-ЯЁ]\.\s?[А-ЯЁ]\."
    room_pattern = r"[Аа]уд\.?\s*[\d\.]+[а-яА-Яa-zA-Z]?"
    test = re.sub(teacher_pattern, "", stripped)
    test = re.sub(room_pattern, "", test)
    test = test.strip(" \n.,;:-_")
    return len(test) < 3 and len(stripped) > 0


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
def list_homework(group_name: str, author_name: str = None):
    db: Session = SessionLocal()
    try:
        query = db.query(Homework).filter(Homework.group_name == group_name)
        if author_name:
            entries = query.filter(
                (Homework.is_shared == True) | (Homework.author_name == author_name)
            ).order_by(Homework.created_at.desc()).all()
        else:
            entries = query.filter(Homework.is_shared == True).order_by(Homework.created_at.desc()).all()
        return {"ok": True, "items": [homework_to_dict(e) for e in entries]}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    finally:
        db.close()


@app.delete("/homework/{homework_id}")
def delete_homework(homework_id: int, author_name: str):
    db: Session = SessionLocal()
    try:
        entry = db.query(Homework).filter(Homework.id == homework_id).first()
        if not entry:
            return {"ok": False, "error": "Не найдено"}
        if entry.author_name != author_name:
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

    pdf_list = parse_schedule_page_pdfs(html)
    pdf_list = pdf_list[:max_weeks_per_group]

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

            for group_name in groups:
                group_lessons = parse_pdf(pdf_url, group_filter=group_name, prefetched_table=table)
                for lesson in group_lessons:
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
        except Exception:
            continue

    return results


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
