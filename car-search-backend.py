from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Optional, Set
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

# Modèles de données
class CarListing(BaseModel):
    site_source: str
    title: str
    make: str
    model: str
    year: int
    price: float
    mileage: int
    fuel_type: str
    location: str
    url: str
    description: str
    posting_date: datetime
    seller_type: str
    images: List[str]

class SearchParams(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    max_mileage: Optional[int] = None
    fuel_type: Optional[str] = None
    location: Optional[str] = None
    body_type: Optional[str] = None
    transmission: Optional[str] = None
    color: Optional[str] = None

class NotificationParams(BaseModel):
    search_params: SearchParams
    email: str

# Configuration de l'application
app = FastAPI()
client = AsyncIOMotorClient("mongodb://localhost:27017")
db = client.car_listings

# Stockage des souscriptions aux notifications
active_subscriptions: Set[NotificationParams] = set()

# Sites sources
SITES = {
    "leboncoin": {
        "search_url": "https://www.leboncoin.fr/recherche?category=2&text={query}",
        "listing_selector": "article.styles_adCard__2YFTi"
    },
    "lacentrale": {
        "search_url": "https://www.lacentrale.fr/listing?makesModels={query}",
        "listing_selector": "div.searchCard"
    }
}

# Scraping asynchrone
async def scrape_site(session, site_name: str, search_params: SearchParams):
    site_config = SITES[site_name]
    query = f"{search_params.make} {search_params.model}".strip()
    
    try:
        async with session.get(site_config["search_url"].format(query=query)) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                listings = []

                for listing_elem in soup.select(site_config["listing_selector"]):
                    try:
                        listing = parse_listing(site_name, listing_elem)
                        if matches_criteria(listing, search_params):
                            listings.append(listing)
                    except Exception as e:
                        print(f"Error parsing listing: {e}")
                
                return listings
    except Exception as e:
        print(f"Error scraping {site_name}: {e}")
        return []

def parse_listing(site_name: str, listing_elem) -> CarListing:
    """Parse HTML element into CarListing object based on site-specific selectors"""
    # Implémentation spécifique pour chaque site

def matches_criteria(listing: CarListing, params: SearchParams) -> bool:
    """Vérifie si l'annonce correspond aux critères de recherche"""
    if params.min_price and listing.price < params.min_price:
        return False
    if params.max_price and listing.price > params.max_price:
        return False
    if params.min_year and listing.year < params.min_year:
        return False
    if params.max_year and listing.year > params.max_year:
        return False
    if params.max_mileage and listing.mileage > params.max_mileage:
        return False
    if params.fuel_type and listing.fuel_type.lower() != params.fuel_type.lower():
        return False
    if params.body_type and listing.body_type.lower() != params.body_type.lower():
        return False
    if params.transmission and listing.transmission.lower() != params.transmission.lower():
        return False
    if params.color and listing.color.lower() != params.color.lower():
        return False
    return True

# Routes API
@app.post("/search")
async def search_cars(params: SearchParams):
    async with aiohttp.ClientSession() as session:
        # Lancer le scraping en parallèle pour tous les sites
        tasks = [
            scrape_site(session, site_name, params)
            for site_name in SITES.keys()
        ]
        results = await asyncio.gather(*tasks)
        
        # Fusionner et trier les résultats
        all_listings = []
        for site_results in results:
            all_listings.extend(site_results)
        
        # Sauvegarder les résultats dans MongoDB
        if all_listings:
            await db.listings.insert_many([listing.dict() for listing in all_listings])
        
        return {"listings": all_listings}

@app.post("/subscribe")
async def subscribe_to_notifications(params: NotificationParams):
    active_subscriptions.add(params)
    return {"message": "Subscription created successfully"}

@app.post("/unsubscribe")
async def unsubscribe_from_notifications(params: NotificationParams):
    if params in active_subscriptions:
        active_subscriptions.remove(params)
        return {"message": "Subscription deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail="Subscription not found")

@app.websocket("/notifications")
async def send_notifications(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            # Vérifier s'il y a de nouvelles annonces correspondant aux critères des abonnés
            new_listings = await check_for_new_listings()
            if new_listings:
                await websocket.send_json({"new_listings": [listing.dict() for listing in new_listings]})
            await asyncio.sleep(60)  # Vérifier toutes les minutes
        except WebSocketDisconnect:
            break

async def check_for_new_listings() -> List[CarListing]:
    new_listings = []
    for subscription in active_subscriptions:
        listings = await search_cars(subscription.search_params)["listings"]
        for listing in listings:
            if not await db.listings.find_one({"url": listing.url}):
                new_listings.append(listing)
                await db.listings.insert_one(listing.dict())
    return new_listings

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
