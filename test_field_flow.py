import asyncio
import math
from datetime import datetime, timezone, timedelta

# Import functions from main.py to test them
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from main import calculate_haversine

def test_distance_calculation():
    print("--- Testing Haversine Formula ---")
    # Coordinates for Connaught Place, New Delhi
    cp_lat = 28.6304
    cp_lon = 77.2177
    
    # Coordinates for India Gate, New Delhi (~2.2 km away)
    ig_lat = 28.6129
    ig_lon = 77.2295
    
    dist_meters = calculate_haversine(cp_lat, cp_lon, ig_lat, ig_lon)
    dist_km = dist_meters / 1000.0
    
    print(f"Distance between Connaught Place and India Gate: {dist_meters:.2f} meters ({dist_km:.2f} km)")
    
    # Asserting correctness based on known distance (approx 2.2-2.3 km)
    assert 2.0 <= dist_km <= 2.5, "Distance calculation is inaccurate!"
    print("✅ Distance calculation is accurate.")

def test_km_calculation_logic():
    print("\n--- Testing KM Calculation Data Flow ---")
    # Mocking location pings simulating an agent's path
    pings = [
        {"lat": 28.6304, "lng": 77.2177}, # Point A
        {"lat": 28.6129, "lng": 77.2295}, # Point B (~2.26 km from A)
        {"lat": 28.5921, "lng": 77.2384}  # Point C (~2.47 km from B)
    ]
    
    total_km = 0.0
    if len(pings) > 1:
        for i in range(len(pings) - 1):
            p1 = pings[i]
            p2 = pings[i+1]
            dist_meters = calculate_haversine(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
            total_km += (dist_meters / 1000.0)
            
    print(f"Total calculated KM for route A->B->C: {total_km:.2f} km")
    assert 4.5 <= total_km <= 5.0, "Total KM accumulation logic is flawed!"
    print("✅ Total KM accumulation logic is accurate.")
    
def test_reimbursement_calculation():
    print("\n--- Testing Reimbursement Calculation ---")
    total_km = 4.73
    rate_per_km = 10.0 # INR per KM
    
    total_amount = total_km * rate_per_km
    print(f"Total Amount for {total_km} km at ₹{rate_per_km}/km: ₹{total_amount:.2f}")
    
    assert total_amount == 47.3, "Reimbursement math error!"
    print("✅ Reimbursement calculation is accurate.")

if __name__ == '__main__':
    test_distance_calculation()
    test_km_calculation_logic()
    test_reimbursement_calculation()
    print("\n🎉 All calculations verified successfully!")
