import asyncio
import logging
import json
import re
from typing import Dict, Any, Optional, Tuple, List
import requests
from bs4 import BeautifulSoup
from config import (
    DEMO_MODE,
    USER_AGENT,
    REQUEST_TIMEOUT,
    YU_PORTAL_URL,
    SIS_SESSION_ID,
    SIS_SESSION_COOKIE,
    STUDENT_ID,
    STUDENT_PASSWORD,
    EMAIL_USER,
    EMAIL_PASSWORD,
    EMAIL_HOST,
    EMAIL_PORT
)
from email_otp import fetch_latest_yu_otp

logger = logging.getLogger(__name__)


class CourseCheckResult:
    def __init__(
        self,
        course_no: str,
        section_no: str,
        course_name: str = "",
        capacity: int = 0,
        registered: int = 0,
        available_seats: int = 0,
        is_available: bool = False,
        instructor: str = "غير محدد",
        schedule_time: str = "غير محدد",
        hall: str = "غير محدد",
        raw_status: str = "UNKNOWN",
        error_message: Optional[str] = None
    ):
        self.course_no = course_no
        self.section_no = str(section_no).strip()
        self.course_name = course_name
        self.capacity = capacity
        self.registered = registered
        self.available_seats = max(0, available_seats)
        self.is_available = is_available or (self.available_seats > 0)
        self.instructor = instructor
        self.schedule_time = schedule_time
        self.hall = hall
        self.raw_status = raw_status
        self.error_message = error_message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "course_no": self.course_no,
            "section_no": self.section_no,
            "course_name": self.course_name,
            "capacity": self.capacity,
            "registered": self.registered,
            "available_seats": self.available_seats,
            "is_available": self.is_available,
            "instructor": self.instructor,
            "schedule_time": self.schedule_time,
            "hall": self.hall,
            "raw_status": self.raw_status,
            "error_message": self.error_message,
        }


def parse_course_symbol_and_num(course_input: str) -> Tuple[str, str]:
    """
    تحليل مدخل المساق وفصل الرمز عن الرقم:
    يدعم:
    - "CS 111L" -> ("CS", "111L")
    - "CS111L"  -> ("CS", "111L")
    - "FT 200"  -> ("FT", "200")
    - "ACC 101" -> ("ACC", "101")
    - "101330"  -> ("", "101330")
    """
    clean = course_input.strip().upper()
    
    # محاولة مطابقة مثل CS 111L أو CS111L أو FT 200 أو ريض 101
    match = re.match(r"^([A-Z\u0621-\u064A]+)\s*(\d+[A-Z\u0621-\u064A]?)$", clean)
    if match:
        return match.group(1), match.group(2)
    
    parts = clean.split()
    if len(parts) == 2:
        return parts[0], parts[1]
    
    return "", clean


class YarmoukScraper:
    """
    محرك فحص شواغر جامعة اليرموك الحي والمباشر 🎓
    يدعم:
    1. الفحص الحي المباشر لجميع شعب ومقاعد جامعة اليرموك
    2. الدخول التلقائي الذكي وتجديد التوكنز وجلسة Oracle APEX الحية باستمرار
    """

    def __init__(self):
        self.session = requests.Session()
        self.session_id = SIS_SESSION_ID or ""
        self.report_req_id = ""
        self.protected_val = ""
        self.salt_val = ""
        
        # إذا تم تمرير كوكي مبدئي
        if SIS_SESSION_COOKIE:
            for cookie in SIS_SESSION_COOKIE.split(";"):
                if "=" in cookie:
                    k, v = cookie.strip().split("=", 1)
                    self.session.cookies.set(k.strip(), v.strip(), domain="sis.yu.edu.jo")

    def auto_relogin(self) -> bool:
        """
        تسجيل الدخول التلقائي في نظام SIS واستخراج معرف التقرير (ajaxIdentifier)
        والتوكنز المحمية (pPageItemsProtected و pSalt) بشكل حي وديناميكي بالكامل.
        """
        if not STUDENT_ID or not STUDENT_PASSWORD:
            logger.warning("لم يتم تعيين STUDENT_PASSWORD في .env للتسجيل التلقائي.")
            return False

        try:
            logger.info("🔄 جاري تجديد جلسة نظام SIS واستخراج التوكنز الحية...")
            login_url = f"{YU_PORTAL_URL}/ords/r/sis/sis/login"
            headers_get = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            r = self.session.get(login_url, headers=headers_get, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(r.text, "html.parser")

            p_instance = soup.find("input", {"id": "pInstance"})
            p_sub_id = soup.find("input", {"id": "pPageSubmissionId"})
            p_salt = soup.find("input", {"id": "pSalt"})
            p_prot = soup.find("input", {"id": "pPageItemsProtected"})

            inst_val = p_instance["value"] if p_instance else ""
            sub_val = p_sub_id["value"] if p_sub_id else ""
            salt_val = p_salt["value"] if p_salt else ""
            prot_val = p_prot["value"] if p_prot else ""

            login_payload = {
                "p_flow_id": "1010",
                "p_flow_step_id": "9999",
                "p_instance": inst_val,
                "p_page_submission_id": sub_val,
                "p_request": "LOGIN",
                "p_reload_on_submit": "S",
                "p_json": json.dumps({
                    "pageItems": {
                        "itemsToSubmit": [
                            {"n": "P9999_USERNAME", "v": STUDENT_ID},
                            {"n": "P9999_PASSWORD", "v": STUDENT_PASSWORD},
                            {"n": "P9999_REMEMBER", "v": ["Y"]}
                        ],
                        "protected": prot_val,
                        "rowVersion": "",
                        "formRegionChecksums": []
                    },
                    "salt": salt_val
                })
            }

            post_url = f"{YU_PORTAL_URL}/ords/wwv_flow.accept?p_context=sis/login/{inst_val}"
            headers_post = {
                "User-Agent": USER_AGENT,
                "Referer": r.url,
                "Origin": YU_PORTAL_URL,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            }
            resp_login = self.session.post(post_url, data=login_payload, headers=headers_post, timeout=REQUEST_TIMEOUT, allow_redirects=True)

            m_new = re.search(r"session=(\d+)", resp_login.text) or re.search(r"session=(\d+)", resp_login.url)
            self.session_id = m_new.group(1) if m_new else inst_val

            # فحص ما إذا كانت الصفحة تتطلب تفعيل رمز التحقق (صفحة 9990 في نظام SIS)
            r_verify_check = self.session.get(
                f"{YU_PORTAL_URL}/ords/r/sis/sis/home?session={self.session_id}",
                headers=headers_get,
                timeout=REQUEST_TIMEOUT
            )
            
            is_otp_page = (
                "تفعيل رمز التحقق" in r_verify_check.text or
                "P9990_OTP_CODE" in r_verify_check.text or
                "page-9990" in r_verify_check.text
            )

            if is_otp_page:
                logger.info("📩 نظام SIS يطلب رمز التحقق (OTP). جاري مراقبة البريد الإلكتروني لاستخراج الرمز تلقائياً...")
                
                if not EMAIL_USER or not EMAIL_PASSWORD:
                    logger.error("❌ لم يتم ضبط EMAIL_USER أو EMAIL_PASSWORD في ملف .env لقراءة رمز التحقق تلقائياً!")
                else:
                    otp_code, otp_err = fetch_latest_yu_otp(
                        email_user=EMAIL_USER,
                        email_password=EMAIL_PASSWORD,
                        imap_host=EMAIL_HOST,
                        imap_port=EMAIL_PORT,
                        max_wait_seconds=60,
                        poll_interval=3
                    )
                    
                    if otp_code:
                        logger.info(f"🔑 تم التقاط رمز التحقق بنجاح: {otp_code} | جاري إرساله للـ SIS...")
                        soup_otp = BeautifulSoup(r_verify_check.text, "html.parser")
                        
                        p_sub_otp = soup_otp.find("input", {"id": "pPageSubmissionId"})
                        p_salt_otp = soup_otp.find("input", {"id": "pSalt"})
                        p_prot_otp = soup_otp.find("input", {"id": "pPageItemsProtected"})
                        
                        sub_otp_val = p_sub_otp["value"] if p_sub_otp else sub_val
                        salt_otp_val = p_salt_otp["value"] if p_salt_otp else salt_val
                        prot_otp_val = p_prot_otp["value"] if p_prot_otp else prot_val

                        otp_payload = {
                            "p_flow_id": "1010",
                            "p_flow_step_id": "9990",
                            "p_instance": self.session_id,
                            "p_page_submission_id": sub_otp_val,
                            "p_request": "CONFIRM_OTP_BTN",
                            "p_reload_on_submit": "S",
                            "p_json": json.dumps({
                                "pageItems": {
                                    "itemsToSubmit": [
                                        {"n": "P9990_OTP_CODE", "v": otp_code},
                                        {"n": "P9990_STEP", "v": "2"},
                                        {"n": "P9990_ACTION", "v": ""},
                                        {"n": "P9990_OTP_METHOD", "v": "EMAIL"}
                                    ],
                                    "protected": prot_otp_val,
                                    "rowVersion": "",
                                    "formRegionChecksums": []
                                },
                                "salt": salt_otp_val
                            })
                        }
                        
                        post_otp_url = f"{YU_PORTAL_URL}/ords/wwv_flow.accept?p_context=sis/%D8%AA%D9%81%D8%B9%D9%8A%D9%84-%D8%B1%D9%85%D8%B2-%D8%A7%D9%84%D8%AA%D8%AD%D9%82%D9%82/{self.session_id}"
                        resp_confirm = self.session.post(
                            post_otp_url,
                            data=otp_payload,
                            headers=headers_post,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True
                        )
                        logger.info("✅ تم إرسال وتأكيد رمز التحقق في نظام SIS.")
                    else:
                        logger.warning(f"⚠️ فشل استخراج رمز التحقق من البريد: {otp_err}")

            logger.info(f"✅ تم الدخول بنجاح! Session ID: {self.session_id}")

            # زيارة صفحة معلومات القاعات للحصول على التوكنز ومعرف التقرير الحي
            class_url = f"{YU_PORTAL_URL}/ords/r/sis/sis/class-rooms-information?session={self.session_id}"
            r_class = self.session.get(class_url, headers=headers_get, timeout=REQUEST_TIMEOUT)
            soup_class = BeautifulSoup(r_class.text, "html.parser")

            p_prot_c = soup_class.find("input", {"id": "pPageItemsProtected"})
            p_salt_c = soup_class.find("input", {"id": "pSalt"})
            self.protected_val = p_prot_c["value"] if p_prot_c else ""
            self.salt_val = p_salt_c["value"] if p_salt_c else ""

            # استخراج معرف تقرير Faceted Search الحي
            m_rep = re.search(r'apex\.widget\.report\.init\(["\']faceted_search["\'],\s*["\']([^"\']+)["\']', r_class.text)
            if not m_rep:
                m_rep = re.search(r'["\']ajaxIdentifier["\']:\s*["\'](UkVHSU9OIFRZUEV-fjI1NDE4MDkwMTU3MjA5NTE1MA[A-Za-z0-9\-\\_]+)["\']', r_class.text)

            if m_rep:
                rep_id_raw = m_rep.group(1)
                rep_id = rep_id_raw.encode().decode('unicode-escape')
                self.report_req_id = f"PLUGIN={rep_id}" if not rep_id.startswith("PLUGIN=") else rep_id
                logger.info("✅ تم استخراج التوكنز ومعرف التقرير الحي بنجاح.")
                return True
            else:
                logger.warning("لم يتم العثور على ajaxIdentifier للتقرير في صفحة معلومات الشعب.")
                return False

        except Exception as e:
            logger.error(f"خطأ أثناء محاولة التجديد التلقائي: {e}")
            return False

    async def check_course(
        self, 
        course_no: str, 
        section_no: str, 
        course_name: str = "",
        user_session_data: Optional[Dict[str, Any]] = None
    ) -> CourseCheckResult:
        """فحص حالة المادة والشعبة بشكل غير متزامن (مع دعم جلسة الطالب الخاصة إن وجدت)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_check_course, course_no, section_no, course_name, True, user_session_data)

    def _sync_check_course(
        self, 
        course_no: str, 
        section_no: str, 
        course_name: str = "", 
        retry_on_fail: bool = True,
        user_session_data: Optional[Dict[str, Any]] = None
    ) -> CourseCheckResult:
        # إذا تم تمرير جلسة خاصة بالطالب وكانت مكتملة
        target_session = self.session
        target_session_id = self.session_id
        target_report_req_id = self.report_req_id
        target_protected_val = self.protected_val
        target_salt_val = self.salt_val

        if user_session_data and user_session_data.get("session_id") and user_session_data.get("report_req_id"):
            u_sess = requests.Session()
            u_cookies = user_session_data.get("cookies") or {}
            for k, v in u_cookies.items():
                u_sess.cookies.set(k, v, domain="sis.yu.edu.jo")
            target_session = u_sess
            target_session_id = user_session_data["session_id"]
            target_report_req_id = user_session_data["report_req_id"]
            target_protected_val = user_session_data.get("protected_val") or ""
            target_salt_val = user_session_data.get("salt_val") or ""
        else:
            # استخدام الجلسة المركزية والتأكد من تهيئتها
            if not self.session_id or not self.report_req_id or not self.protected_val:
                if not self.auto_relogin():
                    if not DEMO_MODE:
                        return CourseCheckResult(
                            course_no=course_no,
                            section_no=section_no,
                            course_name=course_name or course_no,
                            raw_status="ERROR",
                            error_message="تعذر الاتصال ببوابة الجامعة SIS لتسجيل الدخول."
                        )
            target_session = self.session
            target_session_id = self.session_id
            target_report_req_id = self.report_req_id
            target_protected_val = self.protected_val
            target_salt_val = self.salt_val

        cre_code, cre_no = parse_course_symbol_and_num(course_no)
        sec_clean = str(section_no).strip()
        display_code = f"{cre_code} {cre_no}".strip() if cre_code else course_no.strip()

        ajax_url = f"{YU_PORTAL_URL}/ords/wwv_flow.ajax?p_context=sis/class-rooms-information/{target_session_id}"
        class_url = f"{YU_PORTAL_URL}/ords/r/sis/sis/class-rooms-information?session={target_session_id}"

        # تجهيز عناصر البحث
        search_text = ""
        if not cre_code and not cre_no:
            search_text = course_no

        payload = {
            "p_flow_id": "1010",
            "p_flow_step_id": "3",
            "p_instance": target_session_id,
            "p_debug": "",
            "p_request": target_report_req_id,
            "p_json": json.dumps({
                "pageItems": {
                    "itemsToSubmit": [
                        {"n": "P3_SEARCH", "v": search_text},
                        {"n": "P3_CRE_DSCP", "v": ""},
                        {"n": "P3_CRE_CODE", "v": cre_code},
                        {"n": "P3_CRE_NO", "v": str(cre_no)},
                        {"n": "P3_LTR_NAME", "v": ""}
                    ],
                    "protected": target_protected_val,
                    "rowVersion": "",
                    "formRegionChecksums": []
                },
                "salt": target_salt_val
            })
        }

        headers = {
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": YU_PORTAL_URL,
            "Referer": class_url,
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest"
        }

        try:
            resp = target_session.post(ajax_url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
            
            # إذا انتهت الجلسة أو حدث خطأ في التطبيق
            is_error_response = (
                resp.status_code != 200 or 
                "session expired" in resp.text.lower() or 
                "login" in resp.text.lower() or 
                '"error"' in resp.text
            )

            if is_error_response:
                if user_session_data:
                    # في حال كانت جلسة خاصة بالطالب وانتهت، نجرب الفحص بالجلسة المركزية
                    logger.warning("انتهت جلسة الطالب الخاصة، التحويل للجلسة المركزية...")
                    return self._sync_check_course(course_no, section_no, course_name, retry_on_fail=True, user_session_data=None)
                elif retry_on_fail:
                    logger.warning("انتهت الجلسة المركزية، جاري تجديد التوكنز وإعادة المحاولة...")
                    if self.auto_relogin():
                        return self._sync_check_course(course_no, section_no, course_name, retry_on_fail=False)

            soup = BeautifulSoup(resp.text, "html.parser")
            tables = soup.find_all("table")

            # استخراج الصفوف
            matched_course_desc = ""
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                    if len(cells) < 14:
                        continue

                    # استخراج الحقول بالترتيب
                    if cells[0] == "رمز المساق" or cells[4] == "الشعبة":
                        continue

                    matched_course_desc = cells[2]
                    row_sec = cells[4]
                    if row_sec == sec_clean:
                        instructor = cells[11] if len(cells) > 11 else "غير محدد"
                        from_time = cells[12] if len(cells) > 12 else ""
                        to_time = cells[13] if len(cells) > 13 else ""
                        avail_str = cells[15] if len(cells) > 15 else "0"
                        hall = cells[16] if len(cells) > 16 else ""
                        method = cells[17] if len(cells) > 17 else ""

                        # استخراج أيام الدوام
                        days_list = []
                        day_names = ["س", "ح", "ن", "ث", "ر", "خ"]
                        for d_idx, d_char in enumerate(day_names, start=5):
                            if d_idx < len(cells) and cells[d_idx] and cells[d_idx] not in ["", " "]:
                                days_list.append(d_char)

                        days_str = " ".join(days_list)
                        time_str = f"{days_str} {from_time} - {to_time}".strip()
                        full_hall = f"{hall} ({method})".strip(" ()") if method else hall

                        avail_seats = 0
                        digits = re.findall(r"\d+", avail_str)
                        if digits:
                            avail_seats = int(digits[0])

                        is_open = avail_seats > 0
                        status_text = "OPEN" if is_open else "CLOSED"

                        return CourseCheckResult(
                            course_no=f"{cells[0]} {cells[1]}",
                            section_no=sec_clean,
                            course_name=matched_course_desc or course_name,
                            capacity=50,
                            registered=50 - avail_seats if avail_seats <= 50 else 0,
                            available_seats=avail_seats,
                            is_available=is_open,
                            instructor=instructor,
                            schedule_time=time_str or "حسب الجدول",
                            hall=full_hall or "جامعة اليرموك",
                            raw_status=status_text
                        )

            # إذا لم يتم العثور على الشعبة المحددة رغم وجود المادة
            if matched_course_desc:
                return CourseCheckResult(
                    course_no=display_code,
                    section_no=sec_clean,
                    course_name=matched_course_desc,
                    raw_status="SECTION_NOT_FOUND",
                    error_message=f"المادة {display_code} ({matched_course_desc}) مطروحة، لكن الشعبة {sec_clean} غير موجودة."
                )

            # إذا لم يتم العثور على أي صف
            return CourseCheckResult(
                course_no=display_code,
                section_no=sec_clean,
                course_name=course_name or display_code,
                raw_status="NOT_FOUND",
                error_message=f"لم يتم العثور على مادة برمز {display_code} في جدول اليرموك. تأكد من كتابة رمز ورقم المادة بدقة (مثال: CS 111L وليس CS 11L)."
            )

        except Exception as e:
            logger.error(f"خطأ أثناء فحص الشعبة: {e}")
            return CourseCheckResult(
                course_no=display_code,
                section_no=sec_clean,
                course_name=course_name or display_code,
                raw_status="ERROR",
                error_message=f"حدث خطأ أثناء الاتصال بالنظام: {str(e)}"
            )


# ====================================================================
# وظائف تسجيل الدخول التفاعلي للطالب وربط الحساب (Student Login Flow)
# ====================================================================

def login_student_step1(student_id: str, student_password: str) -> Dict[str, Any]:
    """
    الخطوة 1: بدء تسجيل دخول الطالب في نظام SIS
    النتائج المحتملة:
    - {"status": "SUCCESS", "session_id": ..., "cookies": ..., "report_req_id": ..., "protected_val": ..., "salt_val": ...}
    - {"status": "OTP_REQUIRED", "session_id": ..., "session": requests.Session, "otp_tokens": {...}}
    - {"status": "INVALID_CREDENTIALS", "error": "..."}
    - {"status": "ERROR", "error": "..."}
    """
    session = requests.Session()
    headers_get = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    try:
        login_url = f"{YU_PORTAL_URL}/ords/r/sis/sis/login"
        r = session.get(login_url, headers=headers_get, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")

        p_instance = soup.find("input", {"id": "pInstance"})
        p_sub_id = soup.find("input", {"id": "pPageSubmissionId"})
        p_salt = soup.find("input", {"id": "pSalt"})
        p_prot = soup.find("input", {"id": "pPageItemsProtected"})

        inst_val = p_instance["value"] if p_instance else ""
        sub_val = p_sub_id["value"] if p_sub_id else ""
        salt_val = p_salt["value"] if p_salt else ""
        prot_val = p_prot["value"] if p_prot else ""

        login_payload = {
            "p_flow_id": "1010",
            "p_flow_step_id": "9999",
            "p_instance": inst_val,
            "p_page_submission_id": sub_val,
            "p_request": "LOGIN",
            "p_reload_on_submit": "S",
            "p_json": json.dumps({
                "pageItems": {
                    "itemsToSubmit": [
                        {"n": "P9999_USERNAME", "v": student_id.strip()},
                        {"n": "P9999_PASSWORD", "v": student_password.strip()},
                        {"n": "P9999_REMEMBER", "v": ["Y"]}
                    ],
                    "protected": prot_val,
                    "rowVersion": "",
                    "formRegionChecksums": []
                },
                "salt": salt_val
            })
        }

        post_url = f"{YU_PORTAL_URL}/ords/wwv_flow.accept?p_context=sis/login/{inst_val}"
        headers_post = {
            "User-Agent": USER_AGENT,
            "Referer": r.url,
            "Origin": YU_PORTAL_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
        resp_login = session.post(post_url, data=login_payload, headers=headers_post, timeout=REQUEST_TIMEOUT, allow_redirects=True)

        if "اسم المستخدم أو كلمة المرور غير صحيحة" in resp_login.text or "invalid login" in resp_login.text.lower() or "P9999_PASSWORD" in resp_login.text or "login" in resp_login.url.lower() or "p9999" in resp_login.text.lower():
            return {"status": "INVALID_CREDENTIALS", "error": "الرقم الجامعي أو كلمة المرور غير صحيحة."}

        m_new = re.search(r"session=(\d+)", resp_login.text) or re.search(r"session=(\d+)", resp_login.url)
        session_id = m_new.group(1) if m_new else inst_val

        # فحص ما إذا كانت الصفحة تطلب تفعيل رمز التحقق (OTP)
        r_verify_check = session.get(
            f"{YU_PORTAL_URL}/ords/r/sis/sis/home?session={session_id}",
            headers=headers_get,
            timeout=REQUEST_TIMEOUT
        )

        if "اسم المستخدم أو كلمة المرور غير صحيحة" in r_verify_check.text or "login" in r_verify_check.url.lower() or "P9999_USERNAME" in r_verify_check.text:
            return {"status": "INVALID_CREDENTIALS", "error": "الرقم الجامعي أو كلمة المرور غير صحيحة."}

        is_otp_page = (
            "تفعيل رمز التحقق" in r_verify_check.text or
            "P9990_OTP_CODE" in r_verify_check.text or
            "page-9990" in r_verify_check.text
        )

        if is_otp_page:
            soup_otp = BeautifulSoup(r_verify_check.text, "html.parser")
            p_sub_otp = soup_otp.find("input", {"id": "pPageSubmissionId"})
            p_salt_otp = soup_otp.find("input", {"id": "pSalt"})
            p_prot_otp = soup_otp.find("input", {"id": "pPageItemsProtected"})

            otp_tokens = {
                "sub_val": p_sub_otp["value"] if p_sub_otp else sub_val,
                "salt_val": p_salt_otp["value"] if p_salt_otp else salt_val,
                "prot_val": p_prot_otp["value"] if p_prot_otp else prot_val
            }
            return {
                "status": "OTP_REQUIRED",
                "session_id": session_id,
                "session": session,
                "otp_tokens": otp_tokens
            }

        # في حال تم تسجيل الدخول مباشرة بدون طلب OTP
        tokens = _extract_classrooms_tokens(session, session_id)
        if tokens:
            return {
                "status": "SUCCESS",
                "session_id": session_id,
                "cookies": session.cookies.get_dict(),
                "report_req_id": tokens["report_req_id"],
                "protected_val": tokens["protected_val"],
                "salt_val": tokens["salt_val"]
            }
        else:
            return {"status": "INVALID_CREDENTIALS", "error": "الرقم الجامعي أو كلمة المرور غير صحيحة."}

    except Exception as e:
        logger.error(f"خطأ أثناء login_student_step1: {e}")
        return {"status": "ERROR", "error": str(e)}


def login_student_step2_otp(session: requests.Session, session_id: str, otp_tokens: Dict[str, str], otp_code: str) -> Dict[str, Any]:
    """
    الخطوة 2: إرسال رمز التحقق OTP الذي أرسله الطالب وتأكيد تفعيل الجلسة
    """
    headers_post = {
        "User-Agent": USER_AGENT,
        "Referer": f"{YU_PORTAL_URL}/ords/r/sis/sis/home?session={session_id}",
        "Origin": YU_PORTAL_URL,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }

    try:
        otp_clean = re.sub(r"\D", "", otp_code).strip()
        otp_payload = {
            "p_flow_id": "1010",
            "p_flow_step_id": "9990",
            "p_instance": session_id,
            "p_page_submission_id": otp_tokens.get("sub_val", ""),
            "p_request": "CONFIRM_OTP_BTN",
            "p_reload_on_submit": "S",
            "p_json": json.dumps({
                "pageItems": {
                    "itemsToSubmit": [
                        {"n": "P9990_OTP_CODE", "v": otp_clean},
                        {"n": "P9990_STEP", "v": "2"},
                        {"n": "P9990_ACTION", "v": ""},
                        {"n": "P9990_OTP_METHOD", "v": "EMAIL"}
                    ],
                    "protected": otp_tokens.get("prot_val", ""),
                    "rowVersion": "",
                    "formRegionChecksums": []
                },
                "salt": otp_tokens.get("salt_val", "")
            })
        }

        post_otp_url = f"{YU_PORTAL_URL}/ords/wwv_flow.accept?p_context=sis/%D8%AA%D9%81%D8%B9%D9%8A%D9%84-%D8%B1%D9%85%D8%B2-%D8%A7%D9%84%D8%AA%D8%AD%D9%82%D9%82/{session_id}"
        resp_confirm = session.post(
            post_otp_url,
            data=otp_payload,
            headers=headers_post,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if "رمز التحقق غير صحيح" in resp_confirm.text or "invalid otp" in resp_confirm.text.lower() or "P9990_OTP_CODE" in resp_confirm.text:
            return {"status": "INVALID_OTP", "error": "رمز التحقق غير صحيح أو انتهت صلاحيته. يرجى التأكد من كتابته بدقة."}

        # استخراج توكنز جدول الشعب
        tokens = _extract_classrooms_tokens(session, session_id)
        if tokens:
            return {
                "status": "SUCCESS",
                "session_id": session_id,
                "cookies": session.cookies.get_dict(),
                "report_req_id": tokens["report_req_id"],
                "protected_val": tokens["protected_val"],
                "salt_val": tokens["salt_val"]
            }
        else:
            return {"status": "ERROR", "error": "نجح التحقق ولكن تعذر استخراج تقرير الشعب."}

    except Exception as e:
        logger.error(f"خطأ أثناء login_student_step2_otp: {e}")
        return {"status": "ERROR", "error": str(e)}


def _extract_classrooms_tokens(session: requests.Session, session_id: str) -> Optional[Dict[str, str]]:
    """استخراج معرف التقرير والتوكنز لصفحة معلومات القاعات والشعب"""
    try:
        headers_get = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        class_url = f"{YU_PORTAL_URL}/ords/r/sis/sis/class-rooms-information?session={session_id}"
        r_class = session.get(class_url, headers=headers_get, timeout=REQUEST_TIMEOUT)
        soup_class = BeautifulSoup(r_class.text, "html.parser")

        p_prot_c = soup_class.find("input", {"id": "pPageItemsProtected"})
        p_salt_c = soup_class.find("input", {"id": "pSalt"})
        prot_val = p_prot_c["value"] if p_prot_c else ""
        salt_val = p_salt_c["value"] if p_salt_c else ""

        m_rep = re.search(r'apex\.widget\.report\.init\(["\']faceted_search["\'],\s*["\']([^"\']+)["\']', r_class.text)
        if not m_rep:
            m_rep = re.search(r'["\']ajaxIdentifier["\']:\s*["\'](UkVHSU9OIFRZUEV-fjI1NDE4MDkwMTU3MjA5NTE1MA[A-Za-z0-9\-\\_]+)["\']', r_class.text)

        if m_rep:
            rep_id_raw = m_rep.group(1)
            rep_id = rep_id_raw.encode().decode('unicode-escape')
            report_req_id = f"PLUGIN={rep_id}" if not rep_id.startswith("PLUGIN=") else rep_id
            return {
                "report_req_id": report_req_id,
                "protected_val": prot_val,
                "salt_val": salt_val
            }
        return None
    except Exception as e:
        logger.error(f"خطأ أثناء _extract_classrooms_tokens: {e}")
        return None
