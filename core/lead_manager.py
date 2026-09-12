"""
SemiSales AI Agent - Lead Management Module
Handles lead database operations, CSV import, Excel export, and lead discovery.
"""

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.database import Lead, CustomerHistory, get_session


class LeadManager:
    """Manages customer leads: create, query, import, export."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def add_lead(self, data: dict) -> Lead:
        """Add a new lead manually."""
        session = get_session(self.database_url)
        try:
            email = (data.get("email") or "").lower().strip()
            if not email:
                raise ValueError("Email is required for lead")

            # Check for duplicate
            existing = session.query(Lead).filter_by(email=email).first()
            if existing:
                logger.info(f"Lead already exists: {email}")
                # Update with new info
                for key, value in data.items():
                    if value and hasattr(existing, key) and key != "email":
                        setattr(existing, key, value)
                session.commit()
                return existing

            lead = Lead(
                name=data.get("name"),
                company=data.get("company"),
                designation=data.get("designation"),
                email=email,
                phone=data.get("phone"),
                alternate_phone=data.get("alternate_phone"),
                address=data.get("address"),
                city=data.get("city"),
                state=data.get("state"),
                country=data.get("country", "India"),
                pincode=data.get("pincode"),
                website=data.get("website"),
                application=data.get("application"),
                industry=data.get("industry"),
                annual_volume_estimate=data.get("annual_volume_estimate"),
                typical_brands_used=data.get("typical_brands_used", []),
                product_categories=data.get("product_categories", []),
                source=data.get("source", "manual"),
                source_details=data.get("source_details"),
                status=data.get("status", "new"),
                tags=data.get("tags", []),
                notes=data.get("notes"),
            )
            session.add(lead)
            session.commit()
            session.refresh(lead)
            logger.info(f"New lead added: {email} ({lead.company})")
            return lead
        finally:
            session.close()

    def get_lead(self, lead_id: int) -> Optional[Lead]:
        session = get_session(self.database_url)
        try:
            return session.query(Lead).filter_by(id=lead_id).first()
        finally:
            session.close()

    def list_leads(
        self,
        status: Optional[str] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        industry: Optional[str] = None,
        limit: int = 500,
    ) -> list[Lead]:
        session = get_session(self.database_url)
        try:
            query = session.query(Lead)
            if status:
                query = query.filter(Lead.status == status)
            if country:
                query = query.filter(Lead.country == country)
            if city:
                query = query.filter(Lead.city == city)
            if industry:
                query = query.filter(Lead.industry == industry)
            return query.order_by(Lead.updated_at.desc()).limit(limit).all()
        finally:
            session.close()

    def update_lead(self, lead_id: int, updates: dict) -> Optional[Lead]:
        session = get_session(self.database_url)
        try:
            lead = session.query(Lead).filter_by(id=lead_id).first()
            if not lead:
                return None
            for key, value in updates.items():
                if hasattr(lead, key):
                    setattr(lead, key, value)
            session.commit()
            session.refresh(lead)
            return lead
        finally:
            session.close()

    def delete_lead(self, lead_id: int) -> bool:
        session = get_session(self.database_url)
        try:
            lead = session.query(Lead).filter_by(id=lead_id).first()
            if not lead:
                return False
            session.delete(lead)
            session.commit()
            return True
        finally:
            session.close()

    def count_leads(self) -> dict:
        session = get_session(self.database_url)
        try:
            total = session.query(Lead).count()
            new = session.query(Lead).filter_by(status="new").count()
            contacted = session.query(Lead).filter_by(status="contacted").count()
            qualified = session.query(Lead).filter_by(status="qualified").count()
            customers = session.query(Lead).filter_by(status="customer").count()
            india = session.query(Lead).filter_by(country="India").count()
            return {
                "total": total,
                "new": new,
                "contacted": contacted,
                "qualified": qualified,
                "customers": customers,
                "india": india,
            }
        finally:
            session.close()

    def import_from_csv(self, file_bytes: bytes) -> dict:
        """Import leads from a CSV file.
        Expected columns: name, company, email, phone, address, city, state, country, application, industry
        """
        try:
            df = pd.read_csv(io.BytesIO(file_bytes))
        except Exception as e:
            return {"status": "error", "message": f"Could not parse CSV: {e}"}

        df.columns = [str(c).lower().strip() for c in df.columns]
        return self._import_dataframe(df)

    def import_from_excel(self, file_bytes: bytes) -> dict:
        """Import leads from an Excel file."""
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        except Exception as e:
            return {"status": "error", "message": f"Could not parse Excel: {e}"}

        df.columns = [str(c).lower().strip() for c in df.columns]
        return self._import_dataframe(df)

    def _import_dataframe(self, df: pd.DataFrame) -> dict:
        """Import leads from a parsed DataFrame."""
        imported = 0
        skipped = 0
        updated = 0
        errors = []

        for idx, row in df.iterrows():
            try:
                email = str(row.get("email", "")).strip().lower()
                if not email or email == "nan" or "@" not in email:
                    skipped += 1
                    continue

                data = {
                    "name": self._clean(row.get("name")),
                    "company": self._clean(row.get("company")),
                    "designation": self._clean(row.get("designation") or row.get("title")),
                    "email": email,
                    "phone": self._clean(row.get("phone") or row.get("mobile")),
                    "alternate_phone": self._clean(row.get("alternate_phone")),
                    "address": self._clean(row.get("address")),
                    "city": self._clean(row.get("city")),
                    "state": self._clean(row.get("state")),
                    "country": self._clean(row.get("country")) or "India",
                    "pincode": self._clean(row.get("pincode") or row.get("zip")),
                    "website": self._clean(row.get("website")),
                    "application": self._clean(row.get("application")),
                    "industry": self._clean(row.get("industry")),
                    "notes": self._clean(row.get("notes")),
                    "source": "csv_import",
                }

                session = get_session(self.database_url)
                try:
                    existing = session.query(Lead).filter_by(email=email).first()
                    if existing:
                        updated += 1
                    else:
                        imported += 1
                finally:
                    session.close()

                self.add_lead(data)

            except Exception as e:
                errors.append(f"Row {idx}: {e}")

        return {
            "status": "ok",
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:10],
            "total_rows": len(df),
        }

    def export_to_excel(self, output_path: str = None, leads: list[Lead] = None) -> bytes:
        """Export all leads to Excel format. Returns the file bytes."""
        if leads is None:
            leads = self.list_leads(limit=10000)

        data = []
        for lead in leads:
            data.append({
                "ID": lead.id,
                "Name": lead.name or "",
                "Company": lead.company or "",
                "Designation": lead.designation or "",
                "Email": lead.email,
                "Phone": lead.phone or "",
                "Alternate Phone": lead.alternate_phone or "",
                "Address": lead.address or "",
                "City": lead.city or "",
                "State": lead.state or "",
                "Country": lead.country or "",
                "Pincode": lead.pincode or "",
                "Website": lead.website or "",
                "Application": lead.application or "",
                "Industry": lead.industry or "",
                "Annual Volume": lead.annual_volume_estimate or "",
                "Typical Brands Used": ", ".join(lead.typical_brands_used or []),
                "Product Categories": ", ".join(lead.product_categories or []),
                "Source": lead.source or "",
                "Status": lead.status or "",
                "Total Inquiries": lead.total_inquiries or 0,
                "Total Quotes": lead.total_quotes_sent or 0,
                "Total Orders": lead.total_orders or 0,
                "First Contact": lead.first_contact_at.strftime("%Y-%m-%d") if lead.first_contact_at else "",
                "Last Contact": lead.last_contact_at.strftime("%Y-%m-%d") if lead.last_contact_at else "",
                "Notes": lead.notes or "",
            })

        df = pd.DataFrame(data)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Leads", index=False)

            # Auto-adjust column widths
            worksheet = writer.sheets["Leads"]
            for column in worksheet.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if cell.value and len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                worksheet.column_dimensions[column_letter].width = adjusted_width

        output.seek(0)
        file_bytes = output.read()

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(file_bytes)
            logger.info(f"Exported {len(leads)} leads to {output_path}")

        return file_bytes

    def get_customer_history(self, email: str, limit: int = 20) -> list[CustomerHistory]:
        """Get past interactions for a customer email."""
        session = get_session(self.database_url)
        try:
            email = email.lower().strip()
            return session.query(CustomerHistory).filter_by(
                customer_email=email
            ).order_by(CustomerHistory.created_at.desc()).limit(limit).all()
        finally:
            session.close()

    @staticmethod
    def _clean(value) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in ("nan", "none", ""):
            return None
        return text
