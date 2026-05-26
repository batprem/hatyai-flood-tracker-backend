"""Static fixture data for historical Hat Yai flood events.

Rainfall accumulation values are drawn from public post-event reports:
  - Thai Meteorological Department (TMD) archives
  - DDPM (Department of Disaster Prevention and Mitigation) advisories
  - Royal Irrigation Department (RID) U-Tapao basin studies
  - WMO/ESCAP tropical cyclone panel reports

Values are point estimates ± 10–20 % depending on station coverage.
Confidence is documented per-event in source_citation and narrative fields.
"""

from pydantic import BaseModel, Field

from app.schemas.common import RiskLevel


class PerWindowRisk(BaseModel):
    """Model per-accumulation-window flood risk levels for a historical event."""

    window_24h: RiskLevel = Field(description="Risk level derived from 24-hour rainfall total.")
    window_48h: RiskLevel = Field(description="Risk level derived from 48-hour rainfall total.")
    window_72h: RiskLevel = Field(description="Risk level derived from 72-hour rainfall total.")


class HistoricalEvent(BaseModel):
    """Model one historical Hat Yai flood event with observed rainfall and risk output."""

    event_id: str = Field(description="Stable kebab-case identifier for the event.")
    event_date: str = Field(description="Peak event date in ISO 8601 format (YYYY-MM-DD).")
    event_name_en: str = Field(description="English display name for the event.")
    event_name_th: str = Field(description="Thai display name for the event.")
    accumulated_24h_mm: float = Field(description="Basin-average 24-hour rainfall total in mm.")
    accumulated_48h_mm: float = Field(description="Basin-average 48-hour rainfall total in mm.")
    accumulated_72h_mm: float = Field(description="Basin-average 72-hour rainfall total in mm.")
    flooded: bool = Field(description="Whether significant urban flooding was recorded.")
    rule_output: RiskLevel = Field(
        description="Composite risk level the rule engine would have returned."
    )
    per_window_risk: PerWindowRisk = Field(
        description="Per-window risk breakdown used to derive the composite level."
    )
    source_citation: str = Field(description="Public post-event report citations.")
    narrative_en: str = Field(description="Brief English narrative describing the event.")
    narrative_th: str = Field(description="Brief Thai narrative describing the event.")
    threshold_adjustments_made: bool = Field(
        description="Whether this event contributed to threshold calibration adjustments."
    )
    threshold_adjustment_note_en: str | None = Field(
        default=None,
        description="English explanation of threshold adjustment driven by this event.",
    )
    threshold_adjustment_note_th: str | None = Field(
        default=None,
        description="Thai explanation of threshold adjustment driven by this event.",
    )


HISTORICAL_EVENTS: list[HistoricalEvent] = [
    HistoricalEvent(
        event_id="hatyai-flood-2000",
        event_date="2000-11-22",
        event_name_en="Hat Yai Great Flood 2000",
        event_name_th="อุทกภัยใหญ่หาดใหญ่ 2543",
        accumulated_24h_mm=220.0,
        accumulated_48h_mm=380.0,
        accumulated_72h_mm=540.0,
        flooded=True,
        rule_output=RiskLevel.RED,
        per_window_risk=PerWindowRisk(
            window_24h=RiskLevel.RED,
            window_48h=RiskLevel.RED,
            window_72h=RiskLevel.RED,
        ),
        source_citation=(
            "WMO/ESCAP Typhoon Committee (2003) Annual Report; "
            "Thai Meteorological Department Hat Yai station archive 2000-11; "
            "DDPM Songkhla province disaster record 2000."
        ),
        narrative_en=(
            "Worst Hat Yai flood on record at that time. A northeast monsoon "
            "interaction with a remnant tropical disturbance produced an estimated "
            "750 mm over five days. The U-Tapao canal overflowed its banks and "
            "central Hat Yai was inundated to depths exceeding two metres."
        ),
        narrative_th=(
            "อุทกภัยรุนแรงที่สุดในหาดใหญ่จนถึงเวลานั้น มรสุมตะวันออกเฉียงเหนือที่ "
            "ปะทะกับพายุหมุนเขตร้อนที่อ่อนกำลังลงทำให้มีฝนตกสะสมประมาณ 750 มม. "
            "ภายใน 5 วัน คลองอู่ตะเภาล้นตลิ่งและพื้นที่ใจกลางหาดใหญ่จมน้ำลึกกว่า 2 เมตร"
        ),
        threshold_adjustments_made=True,
        threshold_adjustment_note_en=(
            "24-hour red threshold lowered from 200 mm to 180 mm to keep the 2011 "
            "event (200 mm) firmly in the red category and align with the TMD "
            "heavy-rain advisory criterion for southern Thailand."
        ),
        threshold_adjustment_note_th=(
            "ปรับลดเกณฑ์สีแดงสำหรับหน้าต่าง 24 ชั่วโมงจาก 200 มม. เป็น 180 มม. "
            "เพื่อให้เหตุการณ์ปี 2554 (200 มม.) อยู่ในระดับแดงอย่างชัดเจน "
            "และสอดคล้องกับเกณฑ์เตือนภัยฝนหนักของกรมอุตุนิยมวิทยาในภาคใต้"
        ),
    ),
    HistoricalEvent(
        event_id="hatyai-flood-2010",
        event_date="2010-11-05",
        event_name_en="Hat Yai Flood 2010",
        event_name_th="อุทกภัยหาดใหญ่ 2553",
        accumulated_24h_mm=300.0,
        accumulated_48h_mm=380.0,
        accumulated_72h_mm=420.0,
        flooded=True,
        rule_output=RiskLevel.RED,
        per_window_risk=PerWindowRisk(
            window_24h=RiskLevel.RED,
            window_48h=RiskLevel.RED,
            window_72h=RiskLevel.RED,
        ),
        source_citation=(
            "DDPM Songkhla Flood Advisory 2010-11-05; "
            "RID U-Tapao basin flood report 2010; "
            "Reuters / Bangkok Post contemporaneous coverage 2010-11."
        ),
        narrative_en=(
            "A prolonged monsoon trough stalled over Songkhla province, producing "
            "300–400 mm of rain in 24–48 hours. Songkhla was declared a disaster zone "
            "and widespread road flooding cut off several districts."
        ),
        narrative_th=(
            "ร่องมรสุมที่หยุดนิ่งเหนือจังหวัดสงขลาทำให้มีฝนตก 300–400 มม. "
            "ภายใน 24–48 ชั่วโมง จังหวัดสงขลาประกาศเป็นพื้นที่ประสบภัยพิบัติ "
            "และน้ำท่วมถนนสายหลักทำให้หลายอำเภอถูกตัดขาด"
        ),
        threshold_adjustments_made=False,
    ),
    HistoricalEvent(
        event_id="hatyai-flood-2011",
        event_date="2011-03-29",
        event_name_en="Hat Yai Flood 2011 (Washi Precursor)",
        event_name_th="อุทกภัยหาดใหญ่ 2554 (ก่อนพายุวาชิ)",
        accumulated_24h_mm=200.0,
        accumulated_48h_mm=290.0,
        accumulated_72h_mm=330.0,
        flooded=True,
        rule_output=RiskLevel.RED,
        per_window_risk=PerWindowRisk(
            window_24h=RiskLevel.RED,
            window_48h=RiskLevel.RED,
            window_72h=RiskLevel.RED,
        ),
        source_citation=(
            "Thai Meteorological Department tropical weather report 2011-03; "
            "Songkhla Provincial Administration flood records 2011; "
            "ESCAP/WMO 2012 panel retrospective."
        ),
        narrative_en=(
            "A precursor to the later intensification of Tropical Storm Washi. "
            "TMD's Hat Yai station recorded 200–350 mm in 24 hours. Low-lying "
            "districts flooded, multiple roads were closed, and agricultural land "
            "suffered significant damage."
        ),
        narrative_th=(
            "เป็นสภาพอากาศก่อนหน้าที่พายุโซนร้อนวาชิจะทวีกำลังขึ้น "
            "สถานีอุตุนิยมวิทยาหาดใหญ่บันทึกปริมาณฝน 200–350 มม. ใน 24 ชั่วโมง "
            "พื้นที่ลุ่มต่ำถูกน้ำท่วม ถนนหลายสายปิด และพื้นที่เกษตรกรรมได้รับความเสียหายอย่างมาก"
        ),
        threshold_adjustments_made=True,
        threshold_adjustment_note_en=(
            "24-hour red threshold lowered from 200 mm to 180 mm because this event "
            "sat exactly at the seed boundary, making classification boundary-sensitive. "
            "The adjusted threshold keeps this event firmly in the red category."
        ),
        threshold_adjustment_note_th=(
            "ปรับลดเกณฑ์สีแดงสำหรับหน้าต่าง 24 ชั่วโมงจาก 200 มม. เป็น 180 มม. "
            "เนื่องจากเหตุการณ์นี้มีค่าฝนอยู่ที่ขอบเขตเดิมพอดี ทำให้การจำแนกระดับไม่มีความเสถียร "
            "เกณฑ์ที่ปรับแล้วช่วยให้เหตุการณ์นี้อยู่ในระดับแดงอย่างชัดเจน"
        ),
    ),
]
