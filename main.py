from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kerykeion import AstrologicalSubject
from geopy.geocoders import ArcGIS
from timezonefinder import TimezoneFinder

app = FastAPI()

# Initialize our own independent location and timezone finders using ArcGIS
geolocator = ArcGIS()
tf = TimezoneFinder()

class BirthData(BaseModel):
    name: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    city: str
    nation: str

@app.post("/calculate")
async def calculate_chart(data: BirthData):
    try:
        # 1. Manually find the Latitude, Longitude, and Timezone
        loc_query = f"{data.city}, {data.nation}"
        location = geolocator.geocode(loc_query)
        
        if not location:
            raise Exception(f"Could not find coordinates for {loc_query}.")
            
        tz_str = tf.timezone_at(lng=location.longitude, lat=location.latitude)
        
        if not tz_str:
            raise Exception("Could not determine timezone for these coordinates.")

        # 2. Pass the exact math coordinates into Kerykeion
        subject = AstrologicalSubject(
            data.name, 
            data.year, 
            data.month, 
            data.day, 
            data.hour, 
            data.minute, 
            lng=location.longitude, 
            lat=location.latitude, 
            tz_str=tz_str, 
            city=data.city
        )
        
        planet_names = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto", "chiron", "mean_node"]
        house_names = ["first_house", "second_house", "third_house", "fourth_house", "fifth_house", "sixth_house", "seventh_house", "eighth_house", "ninth_house", "tenth_house", "eleventh_house", "twelfth_house"]
        
        full_chart = {
            "planets": {},
            "houses": {}
        }
        
        for p in planet_names:
            obj = getattr(subject, p, None)
            if obj:
                full_chart["planets"][p] = {
                    "sign": obj.get("sign", ""),
                    "house": obj.get("house", ""),
                    "degree": round(obj.get("pos", 0), 2),
                    "absolute_degree": round(obj.get("abs_pos", 0), 2),
                    "retrograde": obj.get("retrograde", False)
                }
                    
        for h in house_names:
            obj = getattr(subject, h, None)
            if obj:
                full_chart["houses"][h] = {
                    "sign": obj.get("sign", ""),
                    "degree": round(obj.get("pos", 0), 2),
                    "absolute_degree": round(obj.get("abs_pos", 0), 2)
                }

        if hasattr(subject, 'aspects'):
            full_chart["aspects"] = subject.aspects()

        return {"status": "success", "data": full_chart}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
