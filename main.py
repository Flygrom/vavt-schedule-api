import io
import re
import requests
import pdfplumber
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def find_group_row_and_col(table, group_filter):
    for row_idx, row in enumerate(table):
        for col_idx, cell in enumerate(row):
            if cell and group_filter == str(cell).strip():
                return row_idx, col_idx
    return None, None


def get_pdf_table(pdf_url: str):
    r = requests.get(pdf_url, headers=HEADERS, timeout=15)
    r.raise_for_status()

    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                return tables[0]
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

    type_match = re.search(
        r"\(([слпрзктСЛПРЗКТ]{1,3}\.?|зачет|зачёт|экз\.?)\)",
        text, re.IGNORECASE
    )
    type_raw = type_match.group(1).lower().replace(".", "") if type_match else None
    lesson_type = {
        "с": "Семинар", "л": "Лекция", "пр": "Практика", "пз": "Практика",
        "з": "Зачёт", "зач": "Зачёт", "зачет": "Зачёт", "зачёт": "Зачёт",
        "экз": "Экзамен"
    }.get(type_raw, type_raw.upper() if type_raw else None)
    if type_match:
        text = text[:type_match.start()] + text[type_match.end():]

    teacher_match = re.search(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.", text)
    teacher = teacher_match.group(0).strip() if teacher_match else None
    if teacher_match:
        text = text[:teacher_match.start()] + text[teacher_match.end():]

    subject = re.sub(r"\s+", " ", text).strip(" \n.,;:-_()")

    if not subject:
        subject = original.strip()

    return {
        "subject": subject,
        "teacher": teacher,
        "room": room,
        "type": lesson_type
    }


def parse_pdf(pdf_url: str, group_filter: str = None):
    table = get_pdf_table(pdf_url)
    if not table:
        return []

    lessons = []

    if group_filter:
        _, group_col = find_group_row_and_col(table, group_filter)
        if group_col is None:
            return []
        target_cols = [group_col]
    else:
        max_cols = max(len(row) for row in table)
        target_cols = list(range(2, max_cols))

    current_day = None
    current_time = None
    seen = set()  # для устранения дублей: (day, timeStart, subject)

    for row in table:
        if not row:
            continue

        first_cell = str(row[0] or "").strip()
        second_cell = str(row[1] or "").strip() if len(row) > 1 else ""

        # День определяем по ПЕРВОЙ строке текста в первой колонке
        if first_cell:
            first_line = first_cell.split("\n")[0].strip()
            if first_line in DAYS:
                current_day = first_line

        time_source = second_cell if second_cell else first_cell
        time_match = re.search(
            r"(\d{1,2}[.:]\d{2})\s*[-–—]\s*(\d{1,2}[.:]\d{2})",
            time_source
        )
        if time_match:
            current_time = (
                time_match.group(1).replace(".", ":"),
                time_match.group(2).replace(".", ":")
            )

        if not current_time:
            continue

        non_empty_cells = [
            (i, str(row[i]).strip())
            for i in range(2, len(row))
            if row[i] and str(row[i]).strip()
        ]

        def add_lesson(cell_text):
            parsed = parse_lesson_cell(cell_text)
            if not parsed:
                return
            key = (current_day, current_time[0], parsed["subject"])
            if key in seen:
                return
            seen.add(key)
            lessons.append({
                "day": current_day,
                "timeStart": current_time[0],
                "timeEnd": current_time[1],
                **parsed
            })

        if len(non_empty_cells) == 1:
            add_lesson(non_empty_cells[0][1])
            continue

        for col in target_cols:
            if col >= len(row):
                continue
            cell_text = row[col]
            if not cell_text or not str(cell_text).strip():
                continue
            add_lesson(str(cell_text))

    return lessons                

@app.get("/")
def root():
    return {"status": "ok", "service": "ВАВТ Расписание API"}


@app.get("/schedule")
def get_schedule(pdf_url: str, group: str = None):
    try:
        lessons = parse_pdf(pdf_url, group)
        return {
            "ok": True,
            "group": group,
            "count": len(lessons),
            "lessons": lessons
        }
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


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
