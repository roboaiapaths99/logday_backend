"""Shared calendar: weekends (Saturday and Sunday by default unless configured) plus configured organization holidays are nonworking."""
import calendar
from datetime import datetime
from database import holidays_collection

DEFAULT_WEEKEND_WEEKDAYS = (5, 6)  # Saturday=5, Sunday=6


def is_working_day(date, days_off=(), weekend_weekdays=DEFAULT_WEEKEND_WEEKDAYS):
    return date.weekday() not in weekend_weekdays and date.day not in days_off


async def month_days_off(org_id, month, weekend_weekdays=DEFAULT_WEEKEND_WEEKDAYS):
    docs = await holidays_collection.find({'organization_id': {'$in': [org_id, 'system_org']},
        '$or': [{'payroll_month': month}, {'date': {'$regex': '^' + month + '-'}}]}).to_list(length=None)
    year, number = map(int, month.split('-'))
    days = {day for day in range(1, calendar.monthrange(year, number)[1] + 1)
            if datetime(year, number, day).weekday() in weekend_weekdays}
    for doc in docs:
        days.update(int(day) for day in doc.get('days_off', []))
        if doc.get('date'):
            days.add(int(str(doc['date'])[:10].split('-')[2]))
    return days
