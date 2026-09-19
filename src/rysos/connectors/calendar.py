"""Google Calendar API Connector with multi-account support."""

from datetime import datetime, date, time, timezone, timedelta
from typing import Any, Optional, List
from pydantic import BaseModel

from rysos.config import settings
from rysos.identity import account_type_for
from rysos.auth.google_auth import GoogleAuthManager, google_auth, AccountSession


class CalendarEvent(BaseModel):
    id: str
    summary: str
    start_time: datetime
    end_time: datetime
    account: str = ""  # e.g. "me@company.com" or "me@gmail.com"
    account_type: str = "corporativo"  # 'corporativo' or 'pessoal'
    is_all_day: bool = False
    attendees: list[str] = []
    meet_link: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None


class CalendarConnector:
    """Connector to interact with Google Calendar API across multiple accounts."""

    def __init__(self, auth_manager: Optional[GoogleAuthManager] = None):
        self.auth_manager = auth_manager or google_auth

    def _find_account(self, account_email: Optional[str]) -> Optional[AccountSession]:
        """Resolves a target account by email, defaulting to the personal one."""
        accounts = self.auth_manager.get_all_accounts()
        if not accounts:
            return None
        if account_email:
            for acc in accounts:
                if acc.email.lower() == account_email.lower():
                    return acc
            return None
        for acc in accounts:
            if account_type_for(acc.email) == "pessoal":
                return acc
        return accounts[0]

    def create_event(
        self,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        account_email: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[List[str]] = None,
    ) -> CalendarEvent:
        """Creates an event on the given (or default personal) account's primary calendar.

        `start_time`/`end_time` must be timezone-aware; sent as RFC3339 with an explicit
        `timeZone` (never a naive value coerced with a trailing "Z").
        """
        acc = self._find_account(account_email)
        if acc is None:
            raise RuntimeError("Nenhuma conta Google conectada para criar o evento.")

        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_time.isoformat(), "timeZone": settings.TIMEZONE},
            "end": {"dateTime": end_time.isoformat(), "timeZone": settings.TIMEZONE},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} if "@" in a else {"displayName": a} for a in attendees]

        created = acc.calendar.events().insert(calendarId="primary", body=body).execute()

        account_type = account_type_for(acc.email)
        return self._parse_event(created, account_email=acc.email, account_type=account_type)

    def fetch_events_for_date(
        self,
        target_date: Optional[date] = None,
    ) -> List[CalendarEvent]:
        """Fetches all calendar events across all authenticated Google accounts for a specific day."""
        accounts = self.auth_manager.get_all_accounts()
        if not accounts:
            return []

        target_date = target_date or date.today()
        start_of_day = datetime.combine(target_date, time.min).isoformat() + "Z"
        end_of_day = datetime.combine(target_date, time.max).isoformat() + "Z"

        all_events: List[CalendarEvent] = []

        for acc in accounts:
            account_type = account_type_for(acc.email)
            try:
                service = acc.calendar
                events_result = service.events().list(
                    calendarId="primary",
                    timeMin=start_of_day,
                    timeMax=end_of_day,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()

                items = events_result.get("items", [])
                for item in items:
                    event = self._parse_event(item, target_date, account_email=acc.email, account_type=account_type)
                    if event:
                        all_events.append(event)
            except Exception:
                continue

        # Sort combined timeline by start time
        all_events.sort(key=lambda x: x.start_time)
        return all_events

    def _parse_event(
        self,
        item: dict[str, Any],
        fallback_date: Optional[date] = None,
        account_email: str = "",
        account_type: str = "corporativo",
    ) -> Optional[CalendarEvent]:
        """Parses a Google Calendar event item."""
        event_id = item.get("id", "")
        summary = item.get("summary", "(Sem título)")
        description = item.get("description", "")
        location = item.get("location", "")

        meet_link = None
        if "hangoutLink" in item:
            meet_link = item["hangoutLink"]
        elif "conferenceData" in item:
            for entry in item["conferenceData"].get("entryPoints", []):
                if entry.get("entryPointType") == "video":
                    meet_link = entry.get("uri")
                    break

        attendees = []
        for att in item.get("attendees", []):
            name = att.get("displayName") or att.get("email", "")
            if name:
                attendees.append(name)

        start_dict = item.get("start", {})
        end_dict = item.get("end", {})

        is_all_day = "date" in start_dict
        if is_all_day:
            date_str = start_dict.get("date")
            try:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                start_dt = datetime.combine(fallback_date or date.today(), time.min)
            end_dt = start_dt + timedelta(days=1)
        else:
            start_str = start_dict.get("dateTime", "")
            end_str = end_dict.get("dateTime", "")
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                start_dt = datetime.now()
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                end_dt = start_dt + timedelta(hours=1)

        return CalendarEvent(
            id=f"{account_email}_{event_id}",
            summary=summary,
            start_time=start_dt,
            end_time=end_dt,
            account=account_email,
            account_type=account_type,
            is_all_day=is_all_day,
            attendees=attendees,
            meet_link=meet_link,
            description=description,
            location=location,
        )
