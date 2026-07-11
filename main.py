import io
import re
import requests
import pdfplumber
from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base

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


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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


# MARK: - PDF-парсинг (без изменений)

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

    teacher_match = re.search(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.", text)
    teacher = teacher_match.group(0).strip() if teacher_match else None
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
    teacher_pattern = r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\."
    room_pattern = r"[Аа]уд\.?\s*[\d\.]+[а-яА-Яa-zA-Z]?"
    test = re.sub(teacher_pattern, "", stripped)
    test = re.sub(room_pattern, "", test)
    test = test.strip(" \n.,;:-_")
    return len(test) < 3 and len(stripped) > 0


def parse_pdf(pdf_url: str, group_filter: str = None):
    table = get_pdf_table(pdf_url)
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

        group_cells = [
            (i, str(row[i]).strip())
            for i in range(first_group_col, len(row))
            if row[i] and str(row[i]).strip()
        ]

        def add_lesson(cell_text, col_index=None):
            if col_index is not None and is_continuation_only(cell_text):
                for prev in reversed(lessons):
                    if prev.get("_col") == col_index and prev["day"] == effective_day:
                        parsed_extra = parse_lesson_cell(cell_text)
                        if parsed_extra:
                            if parsed_extra.get("teacher") and not prev.get("teacher"):
                                prev["teacher"] = parsed_extra["teacher"]
                            if parsed_extra.get("room") and not prev.get("room"):
                                prev["room"] = parsed_extra["room"]
                            prev["timeEnd"] = current_time[1]
                        return
                return

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
                "_col": col_index,
                **parsed
            })

        if len(group_cells) == 1 and group_cells[0][0] == first_group_col:
            add_lesson(group_cells[0][1], first_group_col)
            continue

        for col in target_cols:
            if col >= len(row):
                continue
            cell_text = row[col]
            if not cell_text or not str(cell_text).strip():
                continue
            add_lesson(str(cell_text), col)

    for lesson in lessons:
        lesson.pop("_col", None)

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


@app.post("/homework")
def create_homework(hw: HomeworkCreate):
    from sqlalchemy.orm import Session
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
        return {
            "ok": True,
            "id": entry.id,
            "group_name": entry.group_name,
            "subject": entry.subject,
            "author_name": entry.author_name,
            "text": entry.text,
            "is_shared": entry.is_shared,
            "created_at": entry.created_at.isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@app.get("/homework")
def list_homework(group_name: str, author_name: str = None):
    """
    Возвращает все ОБЩИЕ ДЗ для группы + ЛИЧНЫЕ ДЗ этого автора (если указан).
    """
    from sqlalchemy.orm import Session
    db: Session = SessionLocal()
    try:
        query = db.query(Homework).filter(Homework.group_name == group_name)

        if author_name:
            # Общие для всех ИЛИ личные этого автора
            entries = query.filter(
                (Homework.is_shared == True) | (Homework.author_name == author_name)
            ).order_by(Homework.created_at.desc()).all()
        else:
            entries = query.filter(Homework.is_shared == True).order_by(Homework.created_at.desc()).all()

        return {
            "ok": True,
            "items": [
                {
                    "id": e.id,
                    "group_name": e.group_name,
                    "subject": e.subject,
                    "author_name": e.author_name,
                    "text": e.text,
                    "is_shared": e.is_shared,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ]
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}
    finally:
        db.close()


@app.delete("/homework/{homework_id}")
def delete_homework(homework_id: int, author_name: str):
    """Удалить можно только своё ДЗ (проверка по author_name)."""
    from sqlalchemy.orm import Session
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


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
