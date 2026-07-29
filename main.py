from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kerykeion import AstrologicalSubject

app = FastAPI()

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
        # Generate the complete astrological profile
        subject = AstrologicalSubject(data.name, data.year, data.month, data.day, data.hour, data.minute, data.city, data.nation)
        
        planet_names = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto", "chiron", "mean_node"]
        house_names = ["first_house", "second_house", "third_house", "fourth_house", "fifth_house", "sixth_house", "seventh_house", "eighth_house", "ninth_house", "tenth_house", "eleventh_house", "twelfth_house"]
        
        full_chart = {
            "planets": {},
            "houses": {}
        }
        
        # 1. Extract all planets dynamically
        for p in planet_names:
            obj = getattr(subject, p, None)
            if obj:
                # Kerykeion stores this as a dictionary internally
                full_chart["planets"][p] = {
                    "sign": obj.get("sign", ""),
                    "house": obj.get("house", ""),
                    "degree": round(obj.get("pos", 0), 2),
                    "absolute_degree": round(obj.get("abs_pos", 0), 2),
                    "retrograde": obj.get("retrograde", False)
                }
                    
        # 2. Extract all 12 houses dynamically
        for h in house_names:
            obj = getattr(subject, h, None)
            if obj:
                full_chart["houses"][h] = {
                    "sign": obj.get("sign", ""),
                    "degree": round(obj.get("pos", 0), 2),
                    "absolute_degree": round(obj.get("abs_pos", 0), 2)
                }

        # 3. Attempt to pull native aspects if the library version supports it directly
        if hasattr(subject, 'aspects'):
            full_chart["aspects"] = subject.aspects()

        return {"status": "success", "data": full_chart}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
