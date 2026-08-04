from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kerykeion import AstrologicalSubject
from geopy.geocoders import ArcGIS
from timezonefinder import TimezoneFinder
import google.generativeai as genai
import resend
import gspread
from google.oauth2.service_account import Credentials
import asyncio
import os
import json
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

geolocator = ArcGIS()
tf = TimezoneFinder()

PROMPT_CACHE = {"text": "", "last_fetched": 0}

class BirthData(BaseModel):
    name: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    city: str
    nation: str

class DiagnosticRequest(BaseModel):
    name: str
    email: str
    date: str       
    time: str       
    city: str
    nation: str
    categories: list
    question: str

async def get_coordinates(city, nation, retries=2):
    loc_query = f"{city}, {nation}"
    for attempt in range(retries):
        try:
            location = await asyncio.to_thread(geolocator.geocode, loc_query)
            if location:
                return location
        except Exception:
            await asyncio.sleep(1) 
    raise Exception(f"We couldn't locate '{loc_query}'. Please check the spelling of your birth city and try again.")

async def get_system_prompt():
    current_time = time.time()
    if current_time - PROMPT_CACHE["last_fetched"] < 300 and PROMPT_CACHE["text"]:
        return PROMPT_CACHE["text"]
    
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Settings")
        prompt_text = sheet.acell('A1').value
        
        PROMPT_CACHE["text"] = prompt_text
        PROMPT_CACHE["last_fetched"] = current_time
        return prompt_text
    except Exception as e:
        print("Failed to fetch prompt from Sheets:", e)
        return "You are an elite clinical psychological astrologer. Provide a direct, 1-on-1 diagnostic based on the user's chart."

async def get_chart_data(name, year, month, day, hour, minute, city, nation):
    location = await get_coordinates(city, nation)
    tz_str = await asyncio.to_thread(tf.timezone_at, lng=location.longitude, lat=location.latitude)
    if not tz_str:
        raise Exception("Could not determine timezone for this location.")

    subject = AstrologicalSubject(
        name, year, month, day, hour, minute, 
        lng=location.longitude, lat=location.latitude, tz_str=tz_str, city=city
    )
    
    planets = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto", "chiron"]
    chart = {"planets": {}}
    for p in planets:
        obj = getattr(subject, p, None)
        if obj:
            chart["planets"][p] = {"sign": obj.get("sign", ""), "house": obj.get("house", "")}
    return chart

def background_tasks(data, chart_data, report_text):
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1") 
        
        cats_string = ", ".join(data.categories)
        chart_string = json.dumps(chart_data)
        
        row = [
            data.name, 
            data.date, 
            data.time, 
            f"{data.city}, {data.nation}", 
            data.question, 
            data.email, 
            cats_string, 
            chart_string, 
            report_text
        ]
        sheet.append_row(row)
    except Exception as e:
        print(f"Sheet Logging Error: {e}")

    try:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        resend.Emails.send({
            "from": "Yan <onboarding@resend.dev>",
            "to": [data.email],
            "subject": f"{data.name}, your clinical diagnostic is ready",
            "html": f"<p>Hi {data.name},</p><p>Here is the backup copy of your diagnostic:</p><hr/><p>{report_text.replace(chr(10), '<br/>')}</p>"
        })
    except Exception as e:
        print(f"Email Error: {e}")

@app.get("/")
async def health_check(): return {"status": "awake"}

@app.post("/calculate")
async def calculate_chart(data: BirthData):
    chart = await get_chart_data(data.name, data.year, data.month, data.day, data.hour, data.minute, data.city, data.nation)
    return {"status": "success", "data": chart}

@app.post("/generate-diagnostic")
async def generate_diagnostic(data: DiagnosticRequest, bg_tasks: BackgroundTasks):
    try:
        year, month, day = map(int, data.date.split("-"))
        hour, minute = map(int, data.time.split(":"))

        chart_data = await get_chart_data(data.name, year, month, day, hour, minute, data.city, data.nation)

        system_prompt = await get_system_prompt()
        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-1.5-pro") 
        
        cats_str = ", ".join(data.categories)
        user_prompt = f"User Name: {data.name}\nFocus Areas: {cats_str}\nQuestion: {data.question}\nChart Data: {json.dumps(chart_data)}"
        
        report_text = ""
        for attempt in range(2):
            try:
report_text = ""
        for attempt in range(2):
            try:
                response = await asyncio.to_thread(model.generate_content, f"{system_prompt}\n\n{user_prompt}")
                report_text = response.text
                break
            except Exception as ai_err:
                if attempt == 1: raise Exception(f"Gemini API Error: {str(ai_err)}")
                await asyncio.sleep(2)

    except Exception as e:
        print(f"Diagnostic Error: {e}")
        return {"success": False, "error": str(e)}
