from datetime import datetime
import re
import time

import requests

from ..config import settings


class OCRError(RuntimeError):
    pass


def normalize_transaction(tx):
    required_fields = [
        "status",
        "date_of_transaction",
        "receiver_name",
        "receiver_bank",
        "message",
        "transaction_number",
        "sent_from",
        "utr",
        "receiver_phone_number",
        "amount",
        "upi_method",
        "confidence",
    ]

    normalized = {}
    raw_date = tx.get("date_of_transaction")

    if raw_date and isinstance(raw_date, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw_date
    ):
        normalized["date_of_transaction"] = raw_date
    else:
        normalized["date_of_transaction"] = None

    for field in required_fields:
        if field == "date_of_transaction":
            continue

        value = tx.get(field)

        if field == "amount" and value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None

        if field == "receiver_phone_number" and value is not None:
            value = normalize_indian_phone_number(value)

        normalized[field] = value

    return normalized


def normalize_indian_phone_number(value):
    phone_digits = re.sub(r"\D", "", str(value))

    if not phone_digits:
        return None

    if len(phone_digits) == 10:
        return "+91" + phone_digits

    if len(phone_digits) == 11 and phone_digits.startswith("0"):
        return "+91" + phone_digits[1:]

    if len(phone_digits) == 12 and phone_digits.startswith("91"):
        return "+" + phone_digits

    if len(phone_digits) > 10:
        return "+91" + phone_digits[-10:]

    return None


def call_azure_ocr(image):
    azure_endpoint, azure_key = settings.require_azure()
    analyze_url = f"{azure_endpoint}/vision/v3.2/read/analyze"

    response = requests.post(
        analyze_url,
        headers={
            "Ocp-Apim-Subscription-Key": azure_key,
            "Content-Type": "application/octet-stream",
        },
        data=image,
        timeout=settings.ocr_request_timeout_seconds,
    )

    if response.status_code != 202:
        # Azure includes a useful error code/message for authentication and
        # resource configuration failures. Keep the diagnostic, never headers.
        try:
            error = response.json().get("error", {})
            detail = ": ".join(
                value for value in (error.get("code"), error.get("message")) if value
            )
        except (ValueError, AttributeError):
            detail = ""

        if response.status_code == 401:
            hint = "Check that azure_key belongs to the Azure Vision resource at azure_endpoint."
            detail = f"{detail}. {hint}" if detail else hint

        message = f"OCR request failed with status {response.status_code}"
        if detail:
            message = f"{message}: {detail}"
        raise OCRError(message)

    operation_url = response.headers.get("Operation-Location")
    if not operation_url:
        raise OCRError("OCR response did not include an operation URL")

    start_time = time.monotonic()

    while True:
        if time.monotonic() - start_time > settings.ocr_poll_timeout_seconds:
            raise OCRError("OCR polling timeout")

        result_response = requests.get(
            operation_url,
            headers={"Ocp-Apim-Subscription-Key": azure_key},
            timeout=settings.ocr_request_timeout_seconds,
        )
        result_response.raise_for_status()
        result_json = result_response.json()
        status = result_json.get("status")

        if status == "succeeded":
            return result_json

        if status == "failed":
            raise OCRError("OCR processing failed")

        time.sleep(1)


def receipt_classifier(azure_json):
    for page in azure_json.get("analyzeResult", {}).get("readResults", []):
        for line in page.get("lines", []):
            line_text = line.get("text", "").strip()
            normalized_line = line_text.lower()

            if "payment should now be in" in normalized_line:
                return "whatsapp"

            if line_text == "L":
                return "googlepay"

            if "Payment Complete" in line_text:
                return "axis"

            if "पे" in line_text:
                return "phonepe"

            for word in line.get("words", []):
                word_text = word.get("text", "")

                if word_text == "L":
                    return "googlepay"

                if word_text == "पे":
                    return "phonepe"

    return "unknown"


def redirect_receipt(azure_json, mode_of_receipt):
    parsers = {
        "phonepe": PhonePeReceiptParser,
        "googlepay": GPayReceiptParser,
        "axis": AxisReceiptParser,
        "whatsapp": WhatsAppReceiptParser,
    }
    parser_class = parsers.get(mode_of_receipt)
    if not parser_class:
        raise ValueError(f"Unsupported receipt type: {mode_of_receipt}")
    return parser_class(azure_json)


class AxisReceiptParser:
    AMOUNT_PATTERN = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?"

    def __init__(self, azure_json):
        self.lines = flatten_lines(azure_json)

    def _normalize_amount(self, amount):
        cleaned = re.sub(r"[^\d.]", "", amount)
        return int(float(cleaned))

    def _normalize_date(self, date_str):
        parsed_date = datetime.strptime(date_str, "%d/%m/%Y")
        return parsed_date.strftime("%Y-%m-%d 00:00:00")

    def parse(self):
        data = default_transaction("axis")

        for index, text in enumerate(self.lines):
            if text.upper() == "SENT TO":
                name_parts = []
                next_index = index + 1

                while next_index < len(self.lines):
                    next_text = self.lines[next_index]
                    if "XXXX" in next_text or "AMOUNT" in next_text:
                        break
                    name_parts.append(next_text)
                    next_index += 1

                if name_parts:
                    data["receiver_name"] = " ".join(name_parts)

            if text.upper().startswith("REMARKS"):
                message_parts = self.lines[index + 1 : index + 3]
                data["message"] = " ".join(message_parts)

            if text.upper() == "SENT FROM" and index + 1 < len(self.lines):
                data["sent_from"] = self.lines[index + 1]

            if text.startswith("RRN") and index + 1 < len(self.lines):
                data["utr"] = self.lines[index + 1]

            if text.startswith("DATE") and index + 1 < len(self.lines):
                data["date_of_transaction"] = self._normalize_date(self.lines[index + 1])

            if text.startswith("RECEIPT NO"):
                data["transaction_number"] = text.split(":")[-1].strip()

            if data["amount"] is None and re.fullmatch(self.AMOUNT_PATTERN, text):
                data["amount"] = self._normalize_amount(text)

        return data


class GPayReceiptParser:
    RUPEE_PATTERN = r"₹\s?\d+(?:,\d+)*(?:\.\d+)?"
    UPI_PATTERN = r"[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}"

    def __init__(self, azure_json):
        self.lines = flatten_lines(azure_json)

    def _normalize_amount(self, amount_str):
        cleaned = re.sub(r"[^\d.]", "", amount_str)
        return int(float(cleaned))

    def _normalize_date(self, date_str):
        date_str = date_str.replace(",", "")
        formats = ["%d %B %Y %I:%M %p", "%d %b %Y %I:%M %p"]

        for date_format in formats:
            try:
                parsed_date = datetime.strptime(date_str, date_format)
                return parsed_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        return date_str

    def parse(self):
        data = default_transaction("googlepay")
        amounts_found = []

        for index, text in enumerate(self.lines):
            amount_match = re.search(self.RUPEE_PATTERN, text)
            if amount_match:
                raw_amount = amount_match.group()
                amounts_found.append(raw_amount)
                if not data["amount"]:
                    data["amount"] = self._normalize_amount(raw_amount)

            if text.lower() == "paid to" and index + 1 < len(self.lines):
                data["receiver_name"] = self.lines[index + 1]

                if index + 2 < len(self.lines):
                    next_line = self.lines[index + 2]
                    upi = re.search(self.UPI_PATTERN, next_line)
                    phone = re.search(r"\d{10}", upi.group() if upi else next_line)
                    if phone:
                        data["receiver_phone_number"] = "+91" + phone.group()

            if re.search(r"\d{1,2}\s+\w+\s+20\d{2}", text):
                data["date_of_transaction"] = self._normalize_date(text.replace(",", ""))

        if len(set(amounts_found)) > 1:
            data["confidence"] = 0

        return data


class PhonePeReceiptParser:
    RUPEE_PATTERN = r"₹\s?\d+(?:,\d+)*(?:\.\d+)?"
    PHONE_PATTERN = r"^(?:\+91[-\s]?)?[6-9]\d{9}$"
    UTR_PATTERN = r"UTR[:\s]*([\d]+)"
    TXN_PATTERN = r"^T\d{10,}"

    def __init__(self, azure_json):
        self.lines = flatten_lines(azure_json)

    def _normalize_amount(self, amount_str):
        if not amount_str:
            return None
        cleaned = re.sub(r"[^\d.]", "", amount_str)
        return int(float(cleaned))

    def _normalize_date(self, date_str):
        if not date_str:
            return None

        possible_formats = [
            "%I:%M %p on %d %b %Y",
            "%I:%M %p %d %b %Y",
            "%d %b %Y %I:%M %p",
        ]

        for date_format in possible_formats:
            try:
                parsed_date = datetime.strptime(date_str.strip(), date_format)
                return parsed_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        return date_str

    def parse(self):
        data = default_transaction("phonepe")
        data["status"] = None
        amounts_found = []

        for index, text in enumerate(self.lines):
            if "Transaction Successful" in text:
                data["status"] = "Successful"
                if index + 1 < len(self.lines):
                    data["date_of_transaction"] = self._normalize_date(self.lines[index + 1])

            if text.lower() == "paid to" and index + 1 < len(self.lines):
                name_line = self.lines[index + 1]
                amount_match = re.search(self.RUPEE_PATTERN, name_line)

                if amount_match:
                    raw_amount = amount_match.group()
                    amounts_found.append(raw_amount)
                    data["amount"] = self._normalize_amount(raw_amount)
                    name_line = name_line.split("₹")[0].strip()

                data["receiver_name"] = name_line

                if index + 2 < len(self.lines):
                    next_line = self.lines[index + 2]
                    next_amount_match = re.search(self.RUPEE_PATTERN, next_line)

                    if next_amount_match:
                        raw_amount = next_amount_match.group()
                        amounts_found.append(raw_amount)
                        data["amount"] = self._normalize_amount(raw_amount)
                    elif re.search(self.PHONE_PATTERN, next_line):
                        data["receiver_phone_number"] = next_line
                    elif not next_line.startswith("+") and not next_line.upper().startswith("X"):
                        data["receiver_name"] += " " + next_line

            if text.lower().startswith("banking na") and index + 1 < len(self.lines):
                candidate = self.lines[index + 1]
                if len(candidate) >= 3:
                    data["receiver_bank"] = candidate

            if text.lower() == "sent to" and index + 1 < len(self.lines):
                bank_line = self.lines[index + 1].replace(":", "").strip()
                if len(bank_line) >= 3:
                    data["receiver_bank"] = bank_line

            if text.lower().startswith("message") and index + 1 < len(self.lines):
                data["message"] = self.lines[index + 1]

            if text.lower() == "transaction id" and index + 1 < len(self.lines):
                data["transaction_number"] = self.lines[index + 1]

            if re.match(self.TXN_PATTERN, text):
                data["transaction_number"] = text

            utr_match = re.search(self.UTR_PATTERN, text)
            if utr_match:
                data["utr"] = utr_match.group(1)

            if re.match(r"^X{4,}\d+$", text):
                data["sent_from"] = text[-8:]

            amount_match = re.search(self.RUPEE_PATTERN, text)
            if amount_match:
                amounts_found.append(amount_match.group())

            phone_match = re.match(self.PHONE_PATTERN, text.strip())
            if phone_match:
                phone_digits = re.sub(r"\D", "", phone_match.group())
                valid_phone = normalize_indian_phone_number(phone_digits)

                if valid_phone and phone_digits != str(data.get("utr")) and len(phone_digits) <= 12:
                    data["receiver_phone_number"] = valid_phone

        unique_amounts = set(amounts_found)

        if not data["amount"] and unique_amounts:
            data["amount"] = self._normalize_amount(next(iter(unique_amounts)))

        if len(unique_amounts) > 1:
            data["confidence"] = 0

        if not data["status"]:
            data["status"] = "Successful"

        return data


class WhatsAppReceiptParser:
    RUPEE_PATTERN = r"(?:\u20b9|\u00e2\u201a\u00b9|Rs\.?|INR)\s?\d+(?:,\d+)*(?:\.\d+)?"
    SIGNATURE_PATTERN = "payment should now be in"

    def __init__(self, azure_json):
        self.lines = flatten_lines(azure_json)

    def _normalize_amount(self, amount_str):
        if not amount_str:
            return None
        cleaned = re.sub(r"[^\d.]", "", amount_str)
        return int(float(cleaned))

    def _normalize_date(self, date_str):
        if not date_str:
            return None

        cleaned = date_str.strip().replace(",", "")
        possible_formats = [
            "%d %B %Y %H:%M",
            "%d %b %Y %H:%M",
            "%d %B %Y %I:%M %p",
            "%d %b %Y %I:%M %p",
        ]

        for date_format in possible_formats:
            try:
                parsed_date = datetime.strptime(cleaned, date_format)
                return parsed_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        return None

    def _clean_receiver_name(self, value):
        if not value:
            return None

        cleaned = re.sub(r"\s+", " ", value).strip()
        cleaned = re.sub(r"(?i)'?s\s+bank(?:\s+account)?\b", "", cleaned).strip()
        cleaned = re.sub(r"(?i)\bbank\s+account\b", "", cleaned).strip()
        cleaned = re.sub(r"(?i)\bbank\b", "", cleaned).strip()
        cleaned = re.sub(r"(?i)\baccount\b", "", cleaned).strip()
        return cleaned or None

    def _receiver_from_signature(self, index, text):
        after_signature = re.split(
            self.SIGNATURE_PATTERN,
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[-1].strip()
        name_parts = []

        if after_signature:
            name_parts.append(after_signature)

        next_index = index + 1
        while next_index < len(self.lines):
            next_text = self.lines[next_index].strip()

            if self._normalize_date(next_text):
                break

            if next_text.lower() in {"sent to", "transaction id"}:
                break

            name_parts.append(next_text)

            if re.search(r"(?i)\baccount\b", next_text):
                break

            next_index += 1

        return self._clean_receiver_name(" ".join(name_parts))

    def parse(self):
        data = default_transaction("whatsapp")
        data["status"] = None
        amounts_found = []

        for index, text in enumerate(self.lines):
            normalized_text = text.lower()

            amount_match = re.search(self.RUPEE_PATTERN, text)
            if amount_match:
                raw_amount = amount_match.group()
                amounts_found.append(raw_amount)
                if not data["amount"]:
                    data["amount"] = self._normalize_amount(raw_amount)

            if normalized_text == "completed":
                data["status"] = "Successful"

            if self.SIGNATURE_PATTERN in normalized_text and not data["receiver_name"]:
                data["receiver_name"] = self._receiver_from_signature(index, text)
                if data["receiver_name"]:
                    data["receiver_bank"] = "bank account"

            if normalized_text == "sent to" and index + 1 < len(self.lines):
                data["receiver_name"] = self.lines[index + 1]

            normalized_date = self._normalize_date(text)
            if normalized_date and not data["date_of_transaction"]:
                data["date_of_transaction"] = normalized_date

            if normalized_text == "transaction id" and index + 1 < len(self.lines):
                data["transaction_number"] = self.lines[index + 1]

        unique_amounts = set(amounts_found)

        if not data["amount"] and unique_amounts:
            data["amount"] = self._normalize_amount(next(iter(unique_amounts)))

        if len(unique_amounts) > 1:
            data["confidence"] = 0

        if not data["status"]:
            data["status"] = "Successful"

        return data


def flatten_lines(azure_json):
    lines = []
    for page in azure_json["analyzeResult"]["readResults"]:
        for line in page["lines"]:
            text = line["text"].strip()
            if text:
                lines.append(text)
    return lines


def default_transaction(upi_method):
    return {
        "status": "Successful",
        "date_of_transaction": None,
        "receiver_name": None,
        "amount": None,
        "upi_method": upi_method,
        "receiver_phone_number": None,
        "receiver_bank": None,
        "message": None,
        "transaction_number": None,
        "sent_from": None,
        "utr": None,
        "confidence": 1,
    }
