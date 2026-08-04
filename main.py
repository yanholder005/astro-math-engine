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

# --- MODELS ---
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

class UpdateRequest(BaseModel):
    email: str
    additional_info: str

# --- ADMIN ALERT FUNCTION ---
def send_admin_error_alert(error_message, user_data):
    try:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        html_content = f"""
        <h3>Urgent: Astro Funnel Error</h3>
        <p><strong>Error Details:</strong> {error_message}</p>
        <p><strong>User Data:</strong></p>
        <pre>{json.dumps(user_data, indent=2)}</pre>
        """
        resend.Emails.send({
            "from": "Yan Holder <yan@yanholder.com>", # Verified domain required
            "to": ["yan@yanholder.com"],
            "subject": "⚠️ ALARM: Funnel Generation Failed",
            "html": html_content
        })
    except Exception as e:
        print(f"Failed to send admin alert: {e}")

# --- HELPER FUNCTIONS ---
async def get_coordinates(city, nation, retries=2):
    loc_query = f"{city}, {nation}"
    for attempt in range(retries):
        try:
            location = await asyncio.to_thread(geolocator.geocode, loc_query)
            if location:
                return location
        except Exception:
            await asyncio.sleep(1) 
    raise Exception(f"We couldn't locate '{loc_query}'. Please check the spelling.")

async def get_system_prompt():
    current_time = time.time()
    if current_time - PROMPT_CACHE["last_fetched"] < 300 and PROMPT_CACHE["text"]:
        return PROMPT_CACHE["text"]
    
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = await asyncio.to_thread(gspread.authorize, creds)
        sheet = await asyncio.to_thread(client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet, "Settings")
        prompt_text = await asyncio.to_thread(sheet.acell, 'A1')
        prompt_val = prompt_text.value
        
        PROMPT_CACHE["text"] = prompt_val
        PROMPT_CACHE["last_fetched"] = current_time
        return prompt_val
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
            chart["planets"][p] = {
                "sign": obj.get("sign", ""), 
                "house": obj.get("house", ""),
                "degree": round(obj.get("pos", 0), 2),
                "absolute_degree": round(obj.get("abs_pos", 0), 2)
            }
    return chart

def background_tasks(data, chart_data, report_text):
    # 1. Google Sheets Logging
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1") 
        
        cats_string = ", ".join(data.categories)
        chart_string = json.dumps(chart_data)
        
        # Adding an empty string for Column J so the layout is preserved
        row = [
            data.name, data.date, data.time, f"{data.city}, {data.nation}", 
            data.question, data.email, cats_string, chart_string, report_text, ""
        ]
        sheet.append_row(row)
    except Exception as e:
        print(f"Sheet Logging Error: {e}")
        send_admin_error_alert(f"Failed to log row to Google Sheets: {e}", {"email": data.email})

    # 2. Resend Email Dispatch
    try:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        carrd_sales_link = "https://yanholder.carrd.co/#report"
        
        email_html = f"""
        <p>Hi {data.name},</p>
        <p>Here is the backup copy of your astrology report:</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <p>{report_text.replace(chr(10), '<br/>')}</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <h3>Ready to unlock the complete picture?</h3>
        <p>You've seen your baseline. Now map the rest of your chart in deep detail.</p>
        <a href="{carrd_sales_link}" style="display:inline-block; padding:10px 20px; background:#000; color:#fff; text-decoration:none; border-radius:100px; font-weight:bold;">Get The Complete Blueprint</a>
        """

        resend.Emails.send({
            "from": "Yan Holder <yan@yanholder.com>",
            "reply_to": "yan@yanholder.com", 
            "to": [data.email],
            "subject": f"{data.name}, your astrology report is ready",
            "html": email_html
        })
    except Exception as e:
        print(f"Email Error: {e}")
        send_admin_error_alert(f"Failed to send backup email: {e}", {"email": data.email})

def background_update_sheet(email, extra_info):
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1") 
        
        # Search for email in Column F (Index 6)
        cell = sheet.find(email, in_column=6)
        if cell:
            # Update Column J (Index 10) with the new info
            sheet.update_cell(cell.row, 10, extra_info)
        else:
            send_admin_error_alert(f"Could not find email to update additional info.", {"email": email, "info": extra_info})
    except Exception as e:
        print(f"Update Sheet Error: {e}")
        send_admin_error_alert(f"Failed to update Google Sheet with extra info: {e}", {"email": email})

# --- ROUTES ---
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
        model = genai.GenerativeModel("gemini-3.1-pro-preview") 
        
        cats_str = ", ".join(data.categories)
        user_prompt = f"User Name: {data.name}\nFocus Areas: {cats_str}\nQuestion: {data.question}\nChart Data: {json.dumps(chart_data)}"
        
        report_text = ""
        for attempt in range(2):
            try:
                # Upgraded to async generation for high concurrency
                response = await model.generate_content_async(f"{system_prompt}\n\n{user_prompt}")
                report_text = response.text
                break
            except Exception as ai_err:
                if attempt == 1: raise Exception(f"Gemini API Error: {str(ai_err)}")
                await asyncio.sleep(2)

        bg_tasks.add_task(background_tasks, data, chart_data, report_text)
        return {"success": True, "report": report_text}

    except Exception as e:
        print(f"Diagnostic Error: {e}")
        # Send admin alert silently in background
        bg_tasks.add_task(send_admin_error_alert, str(e), data.dict())
        return {"success": False, "error": str(e)}

@app.post("/update-lead")
async def update_lead(data: UpdateRequest, bg_tasks: BackgroundTasks):
    # Triggers Google Sheet search and update in the background so user doesn't wait
    bg_tasks.add_task(background_update_sheet, data.email, data.additional_info)
    return {"success": True}
