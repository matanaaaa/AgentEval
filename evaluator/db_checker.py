"""
Database Check - 通过 CRM 查询接口验证记录是否真实存在且字段正确

不信 Agent 说的"创建成功"，直接查数据库确认：
1. record_id 对应的记录是否存在
2. 记录中的字段值是否与期望一致
"""

import requests
import warnings

import config

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# CRM 查询接口
SEARCH_URL = config.AGENT_BASE_URL + "/rest/bff/v3.0/neoui/table/search"


class DBChecker:
    """通过 CRM REST API 验证记录"""

    def __init__(self, headers: dict = None):
        self.headers = headers or config.get_headers()

    def verify_record(self, record_id: int, entity_type: str, expected_fields: dict) -> dict:
        """
        验证记录是否存在且字段正确。

        Args:
            record_id: Agent 返回的 created_record_id
            entity_type: 实体类型 apikey（如 "contact"）
            expected_fields: 期望的字段值，如 {"contactName": "陈鑫", "mobile": "13800138000"}

        Returns:
            {
                "exists": bool,        # 记录是否存在
                "fields_correct": bool, # 字段是否全部正确
                "matched": [...],       # 匹配的字段
                "mismatched": [...],    # 不匹配的字段
                "record": {...},        # 实际记录数据
            }
        """
        result = {
            "exists": False,
            "fields_correct": False,
            "matched": [],
            "mismatched": [],
            "missing_in_record": [],
            "record": None,
            "query_error": "",
        }

        # 查询记录
        record, query_error = self._query_by_id(record_id, entity_type)
        if query_error:
            result["query_error"] = query_error
            return result
        if not record:
            return result

        result["exists"] = True
        result["record"] = record

        # 逐字段验证
        if not expected_fields:
            result["fields_correct"] = True
            return result

        for field_key, expected_val in expected_fields.items():
            actual_val = record.get(field_key)

            if actual_val is None:
                result["missing_in_record"].append({
                    "field": field_key,
                    "expected": expected_val,
                })
            elif self._value_match(actual_val, expected_val, field_key, record):
                result["matched"].append(field_key)
            else:
                result["mismatched"].append({
                    "field": field_key,
                    "expected": expected_val,
                    "actual": actual_val,
                })

        result["fields_correct"] = (
            len(result["mismatched"]) == 0
            and len(result["missing_in_record"]) == 0
        )

        return result

    def _query_by_id(self, record_id: int, entity_type: str) -> tuple[dict | None, str]:
        """按 ID 查询单条记录"""
        url = f"{SEARCH_URL}?entityApiKey={entity_type}&skipTotalSize=true"

        payload = {
            "dataView": f"{entity_type}_view_owner",
            "conditions": [
                {
                    "fieldApiKey": "id",
                    "operator": "eq",
                    "value": record_id,
                }
            ],
            "page": {"pageNo": 1, "pageSize": 1},
            "sort": [],
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=15,
                verify=False,
            )
            response.raise_for_status()
            data = response.json()

            records = data.get("data", {}).get("records", [])
            if records:
                return records[0], ""
            return None, ""

        except Exception as e:
            print(f"    [DB Check] 查询失败: {e}")
            return None, str(e)

    def _value_match(self, actual, expected, field_key: str, record: dict = None) -> bool:
        """
        字段值匹配（兼容 CRM 内部编码）

        CRM 返回的数据特点：
        - gender: 数字（1=男，2=女）
        - entityType 等关系字段: 库里存内部 ID（如 4046206098120162 或负数 ID），
          中文名在伴随的 label 字段里（<field>-label / <field>__l / <field>_label）
        - accountId: 嵌套对象 {"id": xxx, "name": "公司名"}
        - 普通字段: 字符串
        """
        actual_str = str(actual).strip() if actual is not None else ""
        expected_str = str(expected).strip()

        # 完全匹配
        if actual_str == expected_str:
            return True

        # 嵌套对象匹配（如 accountId: {"id": xx, "name": "中铁十二局集团"}）
        if isinstance(actual, dict):
            actual_name = actual.get("name", "") or actual.get("label", "")
            if expected_str == str(actual_name):
                return True
            if actual_name and (expected_str in str(actual_name) or str(actual_name) in expected_str):
                return True

        # gender 映射
        if field_key == "gender":
            gender_map = {"1": "男", "2": "女"}
            if gender_map.get(actual_str) == expected_str:
                return True

        # 枚举/选项字段（contactRole 等）：库里存的是裸整数编码，且没有伴随的
        # <field>-label 字段可查。复用 evaluator 的字段值映射表做反向解码，
        # 编码字典统一维护在一处，避免两边各写一份、错位不一致。
        from evaluator.evaluator import FIELD_VALUE_MAPPINGS
        mapping = FIELD_VALUE_MAPPINGS.get(field_key)
        if mapping and actual_str in mapping:
            if expected_str in mapping[actual_str]:
                return True

        # 关系字段（entityType/customerLevel 等）：库里是内部 ID，
        # 用伴随的 label 字段和期望的中文比对。这样任意业务类型都能正确校验，
        # 不必逐个把中文值写进白名单。
        if self._looks_like_internal_id(actual) and record:
            label_val = self._find_label(record, field_key)
            if label_val is not None:
                label_str = str(label_val).strip()
                if label_str == expected_str:
                    return True
                if label_str and (expected_str in label_str or label_str in expected_str):
                    return True

        # 包含匹配（仅对非纯数字实际值，避免 ID 与中文误命中）
        if not self._looks_like_internal_id(actual):
            if expected_str and (expected_str in actual_str or actual_str in expected_str):
                return True

        return False

    @staticmethod
    def _looks_like_internal_id(value) -> bool:
        """判断值是否是 CRM 内部 ID（纯整数，含负数），这类值无法直接和中文比。"""
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        s = str(value).strip()
        return bool(s) and (s.lstrip("-").isdigit())

    @staticmethod
    def _find_label(record: dict, field_key: str):
        """
        在记录里找关系字段对应的 label（中文名）伴随字段。

        CRM 常见命名：<field>-label / <field>__l / <field>_label / <field>Label。
        找不到返回 None。
        """
        for suffix in ("-label", "__l", "_label", "Label", "-l"):
            key = f"{field_key}{suffix}"
            if key in record and record[key] not in (None, ""):
                return record[key]
        return None
