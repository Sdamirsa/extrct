"""Mock payload generator for ExtrCT pipeline prototyping.

Emits a pipeline config whose every step carries its own resolved model config,
plus a batch of synthetic cardiac radiology reports - so gates, routing and
per-step model selection can be built before the Model Gateway (M-04) or the
EHR DB exist.

Model config is nested inside pipeline_config: the pipeline declares defaults,
each step may override them, and every step is emitted with a FULLY RESOLVED
model_config so consumers never re-implement inheritance. The `inherited` flag
records which steps took the default.

All report text is fabricated. No MIMIC, no confidential, no patient data.
"""

import uuid

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, FloatInput, IntInput, MultilineInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

# Fixed namespace so deterministic mode yields identical UIDs across runs.
_NAMESPACE = uuid.UUID("b7f9c2a4-1e6d-4f83-9c2b-0a5d7e3f8c14")

_REPORTS = [
    ("TTE", "Transthoracic echocardiogram. Left ventricle normal in size with mildly reduced systolic function, LVEF 47% by biplane Simpson. Mild global hypokinesis without regional wall motion abnormality. Grade I diastolic dysfunction. Mild mitral regurgitation. Aortic valve trileaflet, no stenosis. Right ventricle normal in size and function. No pericardial effusion."),
    ("TTE", "Transthoracic echocardiogram. Severely dilated left ventricle with LVEF 28%. Akinesis of the mid to apical anterior wall and apex. Moderate functional mitral regurgitation. Left atrium moderately dilated. Estimated PASP 46 mmHg. Small pericardial effusion, no tamponade physiology."),
    ("CMR", "Cardiac MRI with gadolinium. LVEF 41%, LVEDVi 118 mL/m2. Subendocardial late gadolinium enhancement in the inferior and inferolateral segments consistent with prior infarction in the RCA territory, involving approximately 35% of wall thickness. No evidence of microvascular obstruction. Right ventricular function preserved."),
    ("CMR", "Cardiac MRI. Asymmetric septal hypertrophy with maximal wall thickness 19 mm at the basal anteroseptum. LVEF 68%. Patchy mid-wall late gadolinium enhancement at the septal insertion points, approximately 6% of LV mass. Findings consistent with hypertrophic cardiomyopathy. No resting outflow tract obstruction."),
    ("CTA", "CT coronary angiography. Calcium score 412 (Agatston). Ostial to proximal LAD demonstrates mixed plaque with 55 to 70% luminal narrowing. Mid LCx with 30% non-obstructive plaque. RCA dominant, non-obstructive disease throughout. CAD-RADS 4A. Recommend functional testing or invasive evaluation."),
    ("CTA", "CT coronary angiography. Calcium score 0. No coronary plaque identified in any territory. Normal coronary origins and course. CAD-RADS 0. No further cardiac workup indicated on the basis of this study."),
    ("CXR", "Portable chest radiograph, AP upright. Cardiomegaly with cardiothoracic ratio 0.58. Pulmonary vascular redistribution and interstitial edema. Small bilateral pleural effusions, right greater than left. No focal consolidation. Findings consistent with decompensated heart failure."),
    ("CXR", "Chest radiograph, PA and lateral. Heart size within normal limits. Clear lung fields without infiltrate, effusion, or pneumothorax. Sternotomy wires intact and in expected position. Prosthetic aortic valve projected over the aortic root. No acute cardiopulmonary process."),
    ("TEE", "Transesophageal echocardiogram. A 14 mm mobile echodensity is attached to the atrial surface of the posterior mitral leaflet, consistent with vegetation. Moderate to severe mitral regurgitation with posteriorly directed jet. No perivalvular abscess. Left atrial appendage free of thrombus."),
    ("NUC", "Myocardial perfusion SPECT with regadenoson stress. Reversible perfusion defect of moderate size and severity in the mid and apical inferolateral segments, consistent with inducible ischemia in the LCx territory. Transient ischemic dilation ratio 1.02. Post-stress LVEF 54%. Summed stress score 9, summed difference score 6."),
]

_DEFAULT_STEPS = """retrieve
extract | model=qwen2.5:14b temp=0.0
#policy_check
synthesize | temp=0.7
cite"""


class MockPipelinePayload(Component):
    display_name: str = "Prep - Mock Payload"
    description: str = "Synthetic pipeline config with per-step model config, plus cardiac radiology reports."
    documentation: str = "docs/reference/langflow-reference.md"
    icon: str = "flask-conical"
    name: str = "mock_pipeline_payload"

    inputs = [
        MultilineInput(
            name="steps",
            display_name="Pipeline Steps",
            info=(
                "One step per line: NAME [| model=NAME] [| temp=FLOAT]. "
                "Prefix with '#' to emit the step disabled. "
                "Omitted settings inherit the pipeline defaults below."
            ),
            value=_DEFAULT_STEPS,
        ),
        StrInput(
            name="default_model_name",
            display_name="Default Model",
            info="Inherited by any step that does not override it.",
            value="qwen2.5:7b",
        ),
        FloatInput(
            name="default_temperature",
            display_name="Default Temperature",
            value=0.0,
        ),
        DropdownInput(
            name="control_flow",
            display_name="Control Flow",
            info="Switchboard axis: gated sequence vs autonomous loop.",
            options=["gated", "loop"],
            value="gated",
        ),
        IntInput(
            name="num_reports",
            display_name="Number of Reports",
            info="How many synthetic reports to emit. The corpus holds 10; higher values cycle it.",
            value=10,
        ),
        BoolInput(
            name="deterministic",
            display_name="Deterministic UIDs",
            info="On: UIDs derive from the seed, so re-runs are identical. Off: fresh uuid4 each run.",
            value=True,
        ),
        StrInput(
            name="seed",
            display_name="Seed",
            value="extrct-mock-v1",
            advanced=True,
        ),
    ]

    outputs = [
        Output(name="payload", display_name="JSON", method="build_payload", group_outputs=True),
        Output(name="pipeline_config", display_name="Pipeline Config", method="build_pipeline_config", group_outputs=True),
        Output(name="steps_table", display_name="Steps", method="build_steps_table", group_outputs=True),
        Output(name="reports", display_name="Reports", method="build_reports_table", group_outputs=True),
    ]

    # --- helpers -----------------------------------------------------------

    def _uid(self, kind: str, index: int = 0) -> str:
        if self.deterministic:
            return str(uuid.uuid5(_NAMESPACE, f"{self.seed}:{kind}:{index}"))
        return str(uuid.uuid4())

    def _parse_step_line(self, line: str) -> tuple[str, bool, dict]:
        """Split 'NAME | model=X temp=Y' into (name, enabled, overrides)."""
        parts = [p.strip() for p in line.split("|")]
        head = parts[0]
        enabled = not head.startswith("#")
        name = head.lstrip("#").strip()

        overrides: dict[str, str] = {}
        for chunk in parts[1:]:
            for token in chunk.split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    overrides[key.strip().lower()] = value.strip()
        return name, enabled, overrides

    def _steps(self) -> list[dict]:
        default_model = self.default_model_name
        default_temp = float(self.default_temperature)

        steps = []
        for i, raw in enumerate(str(self.steps or "").splitlines()):
            line = raw.strip()
            if not line:
                continue
            name, enabled, ov = self._parse_step_line(line)

            model_name = ov.get("model", default_model)
            try:
                temperature = float(ov["temp"]) if "temp" in ov else default_temp
            except (TypeError, ValueError):
                temperature = default_temp

            steps.append(
                {
                    "id": f"s{i + 1}",
                    "name": name,
                    "enabled": enabled,
                    "model_config": {
                        "uid": self._uid("model_config", i),
                        "model_name": model_name,
                        "temperature": temperature,
                        "inherited": not ov,
                    },
                }
            )
        return steps

    def _pipeline_config(self) -> dict:
        steps = self._steps()
        return {
            "uid": self._uid("pipeline_config"),
            "control_flow": self.control_flow,
            "defaults": {
                "model_name": self.default_model_name,
                "temperature": float(self.default_temperature),
            },
            "steps": steps,
        }

    def _reports(self) -> list[dict]:
        n = max(0, int(self.num_reports or 0))
        out = []
        for i in range(n):
            modality, text = _REPORTS[i % len(_REPORTS)]
            out.append(
                {
                    "uid": self._uid("report", i),
                    "index": i,
                    "modality": modality,
                    "text": text,
                }
            )
        return out

    # --- outputs -----------------------------------------------------------

    def build_payload(self) -> Data:
        pipeline = self._pipeline_config()
        reports = self._reports()
        payload = {
            "pipeline_config": pipeline,
            "medical_report_text": {
                "uid": self._uid("report_batch"),
                "count": len(reports),
                "reports": reports,
            },
        }
        active = sum(1 for s in pipeline["steps"] if s["enabled"])
        self.status = f"{active}/{len(pipeline['steps'])} steps | {len(reports)} reports"
        return Data(data=payload)

    def build_pipeline_config(self) -> Data:
        cfg = self._pipeline_config()
        active = sum(1 for s in cfg["steps"] if s["enabled"])
        self.status = f"{active}/{len(cfg['steps'])} steps enabled ({cfg['control_flow']})"
        return Data(data=cfg)

    def build_steps_table(self) -> DataFrame:
        """Flattened one row per step - the shape gates and routers want."""
        rows = [
            {
                "id": s["id"],
                "name": s["name"],
                "enabled": s["enabled"],
                "model_name": s["model_config"]["model_name"],
                "temperature": s["model_config"]["temperature"],
                "inherited": s["model_config"]["inherited"],
                "model_uid": s["model_config"]["uid"],
            }
            for s in self._steps()
        ]
        self.status = f"{sum(1 for r in rows if r['enabled'])}/{len(rows)} enabled"
        return DataFrame(rows)

    def build_reports_table(self) -> DataFrame:
        reports = self._reports()
        self.status = f"{len(reports)} reports"
        return DataFrame(reports)
