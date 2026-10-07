from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kerykeion import AstrologicalSubject
from geopy.geocoders import ArcGIS
from timezonefinder import TimezoneFinder
from google import genai  # <-- UPDATED IMPORT
import resend
import gspread
from google.oauth2.service_account import Credentials
import asyncio
import os
import json
import time
import urllib.request
import datetime
import random
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

geolocator = ArcGIS(timeout=5)
tf = TimezoneFinder()

PROMPT_CACHE = {"text": "", "last_fetched": 0}
PPP_CACHE = {"prices": {}, "base_price_cents": 0, "last_fetched": 0}
GSPREAD_CLIENT = None  

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
            "from": "Yan Holder <yan@yanholder.com>",
            "to": ["yan@yanholder.com"],
            "subject": "⚠️ ALARM: Funnel Generation Failed",
            "html": html_content
        })
    except Exception as e:
        print(f"Failed to send admin alert: {e}")

# --- HELPER FUNCTIONS ---
def get_gspread_client():
    global GSPREAD_CLIENT
    if GSPREAD_CLIENT is None:
        try:
            creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
            creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            GSPREAD_CLIENT = gspread.authorize(creds)
        except Exception as e:
            print(f"Failed to initialize Google Sheets Client: {e}")
            raise
    return GSPREAD_CLIENT

def exponential_backoff_retry(func, *args, max_retries=4, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep((2 ** attempt) + random.uniform(0, 1))

async def get_coordinates(city, nation, retries=3):
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
        client = await asyncio.to_thread(get_gspread_client)
        sheet = await asyncio.to_thread(client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet, "Settings")
        
        def fetch_cell(): return sheet.acell('A1').value
        prompt_val = await asyncio.to_thread(exponential_backoff_retry, fetch_cell)
        
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

    subject = await asyncio.to_thread(
        AstrologicalSubject,
        name, year, month, day, hour, minute, 
        lng=location.longitude, lat=location.latitude, tz_str=tz_str, city=city,
        online=False
    )

    dt = datetime.datetime(year, month, day, hour, minute)
    dt_future = dt + datetime.timedelta(hours=1)
    
    subject_future = await asyncio.to_thread(
        AstrologicalSubject,
        name + "_future", dt_future.year, dt_future.month, dt_future.day, 
        dt_future.hour, dt_future.minute, 
        lng=location.longitude, lat=location.latitude, tz_str=tz_str, city=city,
        online=False
    )

    now_utc = datetime.datetime.utcnow()
    now_f = now_utc + datetime.timedelta(hours=1)
    
    subject_transit = await asyncio.to_thread(
        AstrologicalSubject,
        "Transit", now_utc.year, now_utc.month, now_utc.day, now_utc.hour, now_utc.minute, 
        lng=0.0, lat=51.5, tz_str="UTC", city="London",
        online=False
    )
    subject_transit_f = await asyncio.to_thread(
        AstrologicalSubject,
        "Transit_F", now_f.year, now_f.month, now_f.day, now_f.hour, now_f.minute, 
        lng=0.0, lat=51.5, tz_str="UTC", city="London",
        online=False
    )

    def deg_to_d_m(deg):
        d = int(deg)
        m = int(round((deg - d) * 60))
        if m == 60:
            d += 1
            m = 0
        return f"{d}°{m:02d}’"

    def get_sect_and_lots(subj):
        asc_obj = getattr(subj, "first_house", None)
        sun_obj = getattr(subj, "sun", None)
        moon_obj = getattr(subj, "moon", None)

        if not (asc_obj and sun_obj and moon_obj):
            return None, None, None

        asc_abs = getattr(asc_obj, "abs_pos", 0) if not isinstance(asc_obj, dict) else asc_obj.get("abs_pos", 0)
        sun_abs = getattr(sun_obj, "abs_pos", 0) if not isinstance(sun_obj, dict) else sun_obj.get("abs_pos", 0)
        moon_abs = getattr(moon_obj, "abs_pos", 0) if not isinstance(moon_obj, dict) else moon_obj.get("abs_pos", 0)

        sun_h = getattr(sun_obj, "house", "") if not isinstance(sun_obj, dict) else sun_obj.get("house", "")
        is_day_chart = any(x in str(sun_h) for x in ["7", "8", "9", "10", "11", "12", "Seventh", "Eighth", "Ninth", "Tenth", "Eleventh", "Twelfth"])
        sect = "Day" if is_day_chart else "Night"

        if is_day_chart:
            fortune_abs = (asc_abs + moon_abs - sun_abs) % 360
            spirit_abs = (asc_abs + sun_abs - moon_abs) % 360
        else:
            fortune_abs = (asc_abs + sun_abs - moon_abs) % 360
            spirit_abs = (asc_abs + moon_abs - sun_abs) % 360

        signs = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

        fortune = {"sign": signs[int(fortune_abs // 30)], "position": fortune_abs % 30, "abs_pos": fortune_abs, "house": str(int(fortune_abs // 30) + 1)}
        spirit = {"sign": signs[int(spirit_abs // 30)], "position": spirit_abs % 30, "abs_pos": spirit_abs, "house": str(int(spirit_abs // 30) + 1)}

        return sect, fortune, spirit

    sect, lot_of_fortune, lot_of_spirit = get_sect_and_lots(subject)

    def get_obj(subj, attr):
        obj = getattr(subj, attr, None)
        
        if not obj and attr == "part_of_fortune":
            return lot_of_fortune
            
        if not obj and attr == "true_node":
            obj = getattr(subj, "mean_node", None)

        if not obj and attr == "vertex":
            v_abs = None
            if hasattr(subj, "_ascmc") and subj._ascmc and len(subj._ascmc) > 3:
                v_abs = subj._ascmc[3]
            elif hasattr(subj, "ascmc") and subj.ascmc and len(subj.ascmc) > 3:
                v_abs = subj.ascmc[3]
            
            if v_abs is not None:
                signs = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]
                return {"sign": signs[int(v_abs / 30)], "position": v_abs % 30, "abs_pos": v_abs}
                
        return obj

    def clean_house_name(h):
        mapping = {
            "First_House": "1st", "Second_House": "2nd", "Third_House": "3rd",
            "Fourth_House": "4th", "Fifth_House": "5th", "Sixth_House": "6th",
            "Seventh_House": "7th", "Eighth_House": "8th", "Ninth_House": "9th",
            "Tenth_House": "10th", "Eleventh_House": "11th", "Twelfth_House": "12th"
        }
        return mapping.get(h, h) if h else ""

    def format_pos(p_name, obj):
        if not obj: return None
        is_dict = isinstance(obj, dict)
        
        sign = obj.get("sign", "") if is_dict else getattr(obj, "sign", "")
        pos = obj.get("position", obj.get("pos", 0)) if is_dict else getattr(obj, "position", getattr(obj, "pos", 0)) 
        house = obj.get("house", "") if is_dict else getattr(obj, "house", "")
        abs_pos = obj.get("abs_pos", None) if is_dict else getattr(obj, "abs_pos", None)
        rx = ", Retrograde" if (obj.get("retrograde", False) if is_dict else getattr(obj, "retrograde", False)) else ""
        
        if not house and abs_pos is not None:
            houses_list = ["first_house", "second_house", "third_house", "fourth_house", "fifth_house", "sixth_house", "seventh_house", "eighth_house", "ninth_house", "tenth_house", "eleventh_house", "twelfth_house"]
            cusps = []
            for h in houses_list:
                ho = getattr(subject, h, None)
                if ho:
                    c = getattr(ho, "abs_pos", ho.get("abs_pos", 0)) if isinstance(ho, dict) else getattr(ho, "abs_pos", 0)
                    cusps.append(c)
            if len(cusps) == 12:
                for i in range(12):
                    c1 = cusps[i]
                    c2 = cusps[(i+1)%12]
                    if (c1 < c2 and c1 <= abs_pos < c2) or (c1 > c2 and (abs_pos >= c1 or abs_pos < c2)):
                        house = str(i+1) + ("st" if i==0 else "nd" if i==1 else "rd" if i==2 else "th")
                        break
        
        house_clean = clean_house_name(house)
        house_str = f", in {house_clean} House" if house_clean else (f", in {house} House" if house else "")
        return f"{p_name} in {sign} {deg_to_d_m(pos)}{rx}{house_str}"

    points = [
        ("Sun", "sun"), ("Moon", "moon"), ("Mercury", "mercury"), ("Venus", "venus"), 
        ("Mars", "mars"), ("Jupiter", "jupiter"), ("Saturn", "saturn"), ("Uranus", "uranus"), 
        ("Neptune", "neptune"), ("Pluto", "pluto"), ("North Node", "true_node"), 
        ("Lilith", "lilith"), ("Chiron", "chiron"), ("Fortune", "part_of_fortune"), 
        ("Vertex", "vertex")
    ]

    lines = []
    
    for d_name, a_name in points:
        obj = get_obj(subject, a_name)
        if obj:
            fmt = format_pos(d_name, obj)
            if fmt: lines.append(fmt)

    if lot_of_spirit:
        lines.append(f"Lot of Spirit in {lot_of_spirit['sign']} {deg_to_d_m(lot_of_spirit['position'])}, in {lot_of_spirit['house']} House")
    if sect:
        lines.append(f"Chart Sect: {sect} Chart")

    angles = [("ASC", "first_house"), ("MC", "tenth_house")]
    for d_name, a_name in angles:
        obj = get_obj(subject, a_name)
        if obj:
            pos = getattr(obj, "position", getattr(obj, "pos", 0)) if not isinstance(obj, dict) else obj.get("position", obj.get("pos", 0))
            sign = getattr(obj, "sign", "") if not isinstance(obj, dict) else obj.get("sign", "")
            lines.append(f"{d_name} in {sign} {deg_to_d_m(pos)}")

    houses_map = [
        ("1st House", "first_house"), ("2nd House", "second_house"), ("3rd House", "third_house"),
        ("4th House", "fourth_house"), ("5th House", "fifth_house"), ("6th House", "sixth_house"),
        ("7th House", "seventh_house"), ("8th House", "eighth_house"), ("9th House", "ninth_house"),
        ("10th House", "tenth_house"), ("11th House", "eleventh_house"), ("12th House", "twelfth_house"),
    ]
    for d_name, a_name in houses_map:
        obj = get_obj(subject, a_name)
        if obj:
            pos = getattr(obj, "position", getattr(obj, "pos", 0)) if not isinstance(obj, dict) else obj.get("position", obj.get("pos", 0))
            sign = getattr(obj, "sign", "") if not isinstance(obj, dict) else obj.get("sign", "")
            lines.append(f"{d_name} in {sign} {deg_to_d_m(pos)}")

    def get_abs_pos(obj):
        if not obj: return None
        return getattr(obj, "abs_pos", 0) if not isinstance(obj, dict) else obj.get("abs_pos", 0)

    h1 = get_obj(subject, "first_house")
    dsc_abs = (get_abs_pos(h1) + 180) % 360 if h1 else None
    h10 = get_obj(subject, "tenth_house")
    ic_abs = (get_abs_pos(h10) + 180) % 360 if h10 else None

    entities = []
    for d_name, a_name in points:
        obj = get_obj(subject, a_name)
        obj_f = get_obj(subject_future, a_name)
        if obj and obj_f:
            entities.append({
                "name": d_name,
                "abs_pos": get_abs_pos(obj),
                "abs_pos_f": get_abs_pos(obj_f),
                "is_luminary": d_name in ["Sun", "Moon"]
            })
            
    for d_name, a_name in angles:
        obj = get_obj(subject, a_name)
        obj_f = get_obj(subject_future, a_name)
        if obj and obj_f:
            entities.append({
                "name": "Ascendant" if d_name == "ASC" else d_name,
                "abs_pos": get_abs_pos(obj),
                "abs_pos_f": get_abs_pos(obj_f),
                "is_luminary": False
            })
            
    if dsc_abs is not None:
        h1_f = get_obj(subject_future, "first_house")
        dsc_abs_f = (get_abs_pos(h1_f) + 180) % 360 if h1_f else dsc_abs
        entities.append({"name": "DSC", "abs_pos": dsc_abs, "abs_pos_f": dsc_abs_f, "is_luminary": False})

    if ic_abs is not None:
        h10_f = get_obj(subject_future, "tenth_house")
        ic_abs_f = (get_abs_pos(h10_f) + 180) % 360 if h10_f else ic_abs
        entities.append({"name": "IC", "abs_pos": ic_abs, "abs_pos_f": ic_abs_f, "is_luminary": False})
            
    aspect_types = [("Conjunction", 0), ("Sextile", 60), ("Square", 90), ("Trine", 120), ("Opposition", 180)]
    
    def get_diff(p1, p2):
        d = abs(p1 - p2)
        return min(d, 360 - d)

    aspects_lines = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            e1, e2 = entities[i], entities[j]
            
            if e1["name"] in ["Ascendant", "MC", "DSC", "IC"] and e2["name"] in ["Ascendant", "MC", "DSC", "IC"]:
                continue
            
            diff = get_diff(e1["abs_pos"], e2["abs_pos"])
            diff_f = get_diff(e1["abs_pos_f"], e2["abs_pos_f"])
            
            max_orb = 10 if (e1["is_luminary"] or e2["is_luminary"]) else 8
            points_names = ["North Node", "Lilith", "Chiron", "Fortune", "Vertex", "Ascendant", "MC", "DSC", "IC"]
            if e1["name"] in points_names or e2["name"] in points_names: max_orb = 6
                
            for asp_name, asp_angle in aspect_types:
                orb_limit = max_orb - (2 if asp_name == "Sextile" else 0)
                orb = abs(diff - asp_angle)
                
                if orb <= orb_limit:
                    orb_f = abs(diff_f - asp_angle)
                    status = "Applying" if orb_f < orb else "Separating"
                    aspects_lines.append(f"{e1['name']} {asp_name} {e2['name']} (Orb: {deg_to_d_m(orb)}, {status})")
                    break 

    lines.extend(aspects_lines)

    transit_entities = []
    transit_points = [
        ("Sun", "sun"), ("Moon", "moon"), ("Mercury", "mercury"), ("Venus", "venus"), 
        ("Mars", "mars"), ("Jupiter", "jupiter"), ("Saturn", "saturn"), ("Uranus", "uranus"), 
        ("Neptune", "neptune"), ("Pluto", "pluto"), ("North Node", "true_node"), ("Chiron", "chiron")
    ]

    for d_name, a_name in transit_points:
        obj = get_obj(subject_transit, a_name)
        obj_f = get_obj(subject_transit_f, a_name)
        if obj and obj_f:
            transit_entities.append({
                "name": d_name,
                "abs_pos": get_abs_pos(obj),
                "abs_pos_f": get_abs_pos(obj_f)
            })

    if transit_entities:
        lines.append("\n=== CURRENT TRANSITS TO NATAL ===")
        for t_ent in transit_entities:
            for n_ent in entities:
                diff = get_diff(t_ent["abs_pos"], n_ent["abs_pos"])
                diff_f = get_diff(t_ent["abs_pos_f"], n_ent["abs_pos"]) 
                
                max_orb = 3 if t_ent["name"] in ["Sun", "Moon"] else 2
                
                for asp_name, asp_angle in aspect_types:
                    orb = abs(diff - asp_angle)
                    if orb <= max_orb:
                        orb_f = abs(diff_f - asp_angle)
                        status = "Applying" if orb_f < orb else "Separating"
                        lines.append(f"Transit {t_ent['name']} {asp_name} Natal {n_ent['name']} (Orb: {deg_to_d_m(orb)}, {status})")
                        break 

    return "\n".join(lines)

def background_tasks(data, chart_data, report_text):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1") 
        
        cats_string = ", ".join(data.categories)
        timestamp_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        
        row = [
            data.name, data.date, data.time, f"{data.city}, {data.nation}", 
            data.question, data.email, cats_string, chart_data, report_text, "", timestamp_str
        ]
        
        def append(): sheet.append_row(row)
        exponential_backoff_retry(append)
    except Exception as e:
        print(f"Sheet Logging Error: {e}")
        send_admin_error_alert(f"Failed to log row to Google Sheets: {e}", {"email": data.email})

    try:
        formatted_report = report_text.replace('\n', '<br/>')
        formatted_report = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', formatted_report)

        resend.api_key = os.environ.get("RESEND_API_KEY")
        email_html = f"""
        <p style="font-family: sans-serif; color: #111;">Hi {data.name},</p>
        <p style="font-family: sans-serif; color: #111;">Here is the backup copy of your astrology report:</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <p style="font-family: sans-serif; color: #111;">{formatted_report}</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <h3 style="font-family: sans-serif; color: #111;">Your baseline is established. Now it is time to look deeper.</h3>
        <p style="font-family: sans-serif; color: #333;">The free report mapped your surface, but it only revealed 10% of your chart.</p>
        <p style="font-family: sans-serif; color: #333;">If you want to understand the exact psychological machinery keeping you stuck, the Full Astro Report will hand you the manual. This full 15-page report exposes the exact psychological machinery quietly keeping you stuck, and hands you the non-delusional blueprint to dismantle it. Written with the precision of a clinical psychologist and the depth of an evolutionary astrologer, this is the definitive manual for your life’s trajectory. Plus, after checkout you get to ask as many additional questions as you like and the report will also map out the exact transits hitting your chart over the next six months so you know exactly what is coming.</p>
        <br/>
        <a href="https://yanholder.com/#order-full" style="display:inline-block; padding:10px 20px; background:#000; color:#fff; text-decoration:none; border-radius:100px; font-weight:bold;">Unlock My Full Astro Report</a>
        """

        resend.Emails.send({
            "from": "Yan Holder <yan@yanholder.com>",
            "reply_to": "yan@yanholder.com", 
            "to": [data.email],
            "subject": f"{data.name}, here is a copy of your astrological report.",
            "html": email_html
        })
    except Exception as e:
        print(f"Email Error: {e}")
        send_admin_error_alert(f"Failed to send backup email: {e}", {"email": data.email})

def background_update_sheet(email, extra_info):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1") 
        
        def find_and_update():
            cell = sheet.find(email, in_column=6)
            if cell:
                sheet.update_cell(cell.row, 10, extra_info)
            else:
                send_admin_error_alert(f"Could not find email to update additional info.", {"email": email, "info": extra_info})
        
        exponential_backoff_retry(find_and_update)
    except Exception as e:
        print(f"Update Sheet Error: {e}")
        send_admin_error_alert(f"Failed to update Google Sheet with extra info: {e}", {"email": email})

# --- THE AUTOMATED EMAIL SEQUENCE WORKER ---
async def process_sequence_emails():
    try:
        client = get_gspread_client()
        sheet_id = os.environ.get("GOOGLE_SHEET_ID")
        leads_sheet = client.open_by_key(sheet_id).worksheet("Sheet1")
        buyers_sheet = client.open_by_key(sheet_id).worksheet("PaidReports")

        leads = leads_sheet.get_all_values()
        buyers = buyers_sheet.get_all_values()

        buyer_emails = {row[1].strip().lower() for row in buyers if len(row) > 1}
        now_utc = datetime.datetime.utcnow()
        
        cells_to_update = []

        for i, row in enumerate(leads):
            if len(row) < 11 or not row[10]:
                continue
            
            name = row[0]
            email = row[5].strip().lower()
            timestamp_str = row[10]
            
            # Index 8 is the original free report text from column 9
            report_text = row[8] if len(row) > 8 else "" 

            if email in buyer_emails:
                continue

            try:
                ts = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            except:
                continue
            
            hours_elapsed = (now_utc - ts).total_seconds() / 3600

            # Ensure row length supports sequence tracking up to column 13 (M)
            while len(row) < 14:
                row.append("")
            
            seq1 = row[11]
            seq2 = row[12]
            
            step_to_send = 0
            col_to_update = 0

            # 44 Hour Logic (20 hours after 24h limit)
            if hours_elapsed >= 44 and seq1 == "SENT" and not seq2:
                step_to_send = 2
                col_to_update = 13
            # 24 Hour Logic
            elif hours_elapsed >= 24 and not seq1:
                step_to_send = 1
                col_to_update = 12

            if step_to_send > 0:
                try:
                    resend.api_key = os.environ.get("RESEND_API_KEY")
                    
                    if step_to_send == 1:
                        formatted_report = report_text.replace('\n', '<br/>')
                        formatted_report = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', formatted_report)
                        subject_line = f"{name}, Action required: Your chart file will be erased tomorrow"
                        
                        email_html = f"""
                        <p style="font-family: sans-serif; color: #111;">Hi {name},</p>
                        <p style="font-family: sans-serif; color: #111;">I am reaching out because your file is scheduled for deletion in exactly 24 hours.</p>
                        <p style="font-family: sans-serif; color: #111;">We wipe all birth data and baseline reports from our servers 48 hours after generation to protect your privacy. Once the timer runs out, your full diagnostic will be lost permanently.</p>
                        <p style="font-family: sans-serif; color: #111;">You already took the time to generate the free reading. Stopping here means leaving the actual answers behind. The baseline reading barely scratched the surface. The full 15-page report exposes the primordial engine steering your life. It identifies your karmic blind spots, decodes the actual reason your past relationships failed, and hands you the mathematical formula for your financial growth.</p>
                        <p style="font-family: sans-serif; color: #111;">Leaving that kind of clarity on the table when it is sitting right in front of you is a massive missed opportunity. You have nothing to lose but the blind spot.</p>
                        <p style="font-family: sans-serif; color: #111;">Here is a backup copy of your baseline report for your records before it disappears:</p>
                        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
                        <p style="font-family: sans-serif; color: #111;">{formatted_report}</p>
                        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
                        <h3 style="font-family: sans-serif; color: #111;">You have 24 hours to secure your 15-page Personal Astro Report and 6-month forecast.</h3>
                        <a href="https://yanholder.com/#order-full" style="display:inline-block; padding:10px 20px; background:#000; color:#fff; text-decoration:none; border-radius:100px; font-weight:bold;">Unlock My Full Astro Report</a>
                        """
                    
                    elif step_to_send == 2:
                        subject_line = f"{name}, Final notice: Wiping your chart data"
                        
                        email_html = f"""
                        <p style="font-family: sans-serif; color: #111;">Hi {name},</p>
                        <p style="font-family: sans-serif; color: #111;">This is the last time you will hear from me.</p>
                        <p style="font-family: sans-serif; color: #111;">Your chart data decays in a few hours. After that, your file is permanently wiped from our system and we will not email you again.</p>
                        <p style="font-family: sans-serif; color: #111;">If you want the full 15-page clinical breakdown of your relationship wiring, wealth blocks, and psychological shadow, you need to grab it right now.</p>
                        <p style="font-family: sans-serif; color: #111;">I will not try to convince you further. You either want the non-delusional truth about your life trajectory or you do not.</p>
                        <br/>
                        <a href="https://yanholder.com/#order-full" style="display:inline-block; padding:10px 20px; background:#000; color:#fff; text-decoration:none; border-radius:100px; font-weight:bold;">Unlock My Full Astro Report</a>
                        """

                    resend.Emails.send({
                        "from": "Yan Holder <yan@yanholder.com>",
                        "reply_to": "yan@yanholder.com",
                        "to": [email],
                        "subject": subject_line,
                        "html": email_html
                    })
                    
                    # Track successful send for batch updating the Google Sheet
                    cells_to_update.append(gspread.Cell(row=i + 1, col=col_to_update, value="SENT"))
                    
                    # Rate limiting safeguard: Never exceed Resend API limits
                    await asyncio.sleep(0.5) 
                    
                    # Batch Update in Chunks of 20 to protect Google Sheets Quota
                    if len(cells_to_update) >= 20:
                        def batch_update(): leads_sheet.update_cells(cells_to_update)
                        exponential_backoff_retry(batch_update)
                        cells_to_update.clear()
                    
                except Exception as ex:
                    print(f"Failed to process sequence step {step_to_send} for {email}: {ex}")

        # Final Batch Update for any remaining cells
        if cells_to_update:
            def batch_update_final(): leads_sheet.update_cells(cells_to_update)
            exponential_backoff_retry(batch_update_final)

    except Exception as e:
        print(f"Sequence Scanner Error: {e}")

# --- ROUTES ---
@app.get("/")
async def health_check(): return {"status": "awake"}

@app.get("/trigger-sequence")
async def trigger_sequence(bg_tasks: BackgroundTasks):
    bg_tasks.add_task(process_sequence_emails)
    return {"status": "Sequence scanner initiated in background."}

@app.get("/get-ppp-price")
async def get_ppp_price(country: str = None):
    if not country:
        return {"error": "Country code required"}

    current_time = time.time()
    if not PPP_CACHE["prices"] or (current_time - PPP_CACHE["last_fetched"]) > 3600:
        token = os.environ.get("GUMROAD_ACCESS_TOKEN")
        product_id = os.environ.get("GUMROAD_PRODUCT_ID")
        
        if not token or not product_id:
            return {"error": "Server missing Gumroad credentials"}
            
        url = f"https://api.gumroad.com/v2/products/{product_id}?access_token={token}"
        
        try:
            def fetch_gumroad():
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.loads(response.read().decode())
            
            data = await asyncio.to_thread(fetch_gumroad)
            
            if data.get("success") and "product" in data:
                PPP_CACHE["prices"] = data["product"].get("purchasing_power_parity_prices", {})
                PPP_CACHE["base_price_cents"] = data["product"].get("price", 0)
                PPP_CACHE["last_fetched"] = current_time
            else:
                return {"error": "Could not read Gumroad PPP data"}
        except Exception as e:
            print(f"Gumroad API Error: {e}")
            return {"error": "Internal Server Error"}

    ppp_cents = PPP_CACHE["prices"].get(country)
    if ppp_cents and ppp_cents < PPP_CACHE["base_price_cents"]:
        return {"discountExists": True, "price": ppp_cents / 100}
    return {"discountExists": False}

@app.post("/calculate")
async def calculate_chart(data: BirthData):
    chart = await get_chart_data(data.name, data.year, data.month, data.day, data.hour, data.minute, data.city, data.nation)
    return {"status": "success", "data": chart}

@app.post("/generate-diagnostic")
async def generate_diagnostic(data: DiagnosticRequest, bg_tasks: BackgroundTasks):
    try:
        year, month, day = map(int, data.date.split("-"))
        hour, minute = map(int, data.time.split(":"))
        
        formatted_dob = datetime.date(year, month, day).strftime("%B %d, %Y")
        data.date = formatted_dob 

        now_date = datetime.datetime.utcnow()
        age = now_date.year - year - ((now_date.month, now_date.day) < (month, day))
        
        prof_num = (age % 12) + 1
        suffixes = {1: 'st', 2: 'nd', 3: 'rd'}
        suffix = suffixes.get(prof_num if prof_num < 20 else prof_num % 10, 'th')
        profection_house = f"{prof_num}{suffix} House"

        chart_data = await get_chart_data(data.name, year, month, day, hour, minute, data.city, data.nation)

        system_prompt = await get_system_prompt()
        
        # --- MIGRATED API CLIENT CREATION ---
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        
        cats_str = ", ".join(data.categories)
        current_date_str = now_date.strftime("%B %d, %Y")
        
        user_prompt = f"User Name: {data.name}\nDate of Birth: {formatted_dob}\nCurrent Date: {current_date_str}\nCurrent Age: {age}\nCurrent Profection Year: {profection_house}\nFocus Areas: {cats_str}\nQuestion: {data.question}\nChart Data:\n{chart_data}"
        
        # We merge the system prompt and the user input to emulate the old behavior
        full_input = f"{system_prompt}\n\n{user_prompt}"
        
        report_text = ""
        for attempt in range(3):
            try:
                # --- MIGRATED GENERATE CALL ---
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-3.5-flash-lite", 
                    contents=full_input,
                )
                report_text = response.text
                break
            except Exception as ai_err:
                if attempt == 2: raise Exception(f"Gemini API Error: {str(ai_err)}")
                await asyncio.sleep(2)

        bg_tasks.add_task(background_tasks, data, chart_data, report_text)
        return {"success": True, "report": report_text, "chart": chart_data}

    except Exception as e:
        print(f"Diagnostic Error: {e}")
        bg_tasks.add_task(send_admin_error_alert, str(e), data.dict())
        return {"success": False, "error": str(e)}

@app.post("/update-lead")
async def update_lead(data: UpdateRequest, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(background_update_sheet, data.email, data.additional_info)
    return {"success": True}
