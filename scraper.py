"""
SUNARP Vehicle Consultation Scraper

Uses pydoll to automate Chrome browser and bypass Cloudflare protection.
Intercepts API responses to get vehicle data directly.
"""

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydoll.browser import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.protocol.network.events import NetworkEvent
from pydoll.protocol.page.events import PageEvent


# Configuration
SUNARP_URL = "https://consultavehicular.sunarp.gob.pe/consulta-vehicular/inicio"
SUNARP_API_URL = "api-gateway.sunarp.gob.pe"
SUNARP_API_ENDPOINT = "getDatosVehiculo"
DOWNLOADS_DIR = Path(__file__).parent / "downloads"
CHROMIUM_CANDIDATE_PATHS = [
    os.getenv("SUNARP_CHROMIUM_PATH", "").strip(),
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]

def _env_float(name: str, default: float) -> float:
    """Read float from environment with safe fallback."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read integer from environment with safe fallback."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _positive_float(value: float, fallback: float, minimum: float = 0.1) -> float:
    """Return positive float with minimum guard and fallback."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return fallback
    if val < minimum:
        return fallback
    return val


def _positive_int(value: int, fallback: int, minimum: int = 1) -> int:
    """Return positive integer with minimum guard and fallback."""
    try:
        val = int(value)
    except (TypeError, ValueError):
        return fallback
    if val < minimum:
        return fallback
    return val


# Baseline watchdogs (seconds)
PAGE_LOAD_TIMEOUT = _env_float("SUNARP_PAGE_LOAD_TIMEOUT", 60)
CLOUDFLARE_TIMEOUT = _env_float("SUNARP_CLOUDFLARE_TIMEOUT", 180)
RESULT_TIMEOUT = _env_float("SUNARP_RESULT_TIMEOUT", 90)
CAPTCHA_WAIT_TIMEOUT = _env_float("SUNARP_CAPTCHA_WAIT_TIMEOUT", 20)

# Latency profile (dynamic, progress-aware)
FAST_TARGET_SECONDS = _env_float("SUNARP_FAST_TARGET_SECONDS", 12.0)
TOTAL_BUDGET_SECONDS = _env_float("SUNARP_TOTAL_BUDGET_SECONDS", 18.0)
TOTAL_BUDGET_SLOW_SECONDS = _env_float("SUNARP_TOTAL_BUDGET_SLOW_SECONDS", 75.0)

GATE_INITIAL_SECONDS = _env_float("SUNARP_GATE_INITIAL_SECONDS", 8.0)
GATE_RETRY_SECONDS = _env_float("SUNARP_GATE_RETRY_SECONDS", 4.0)
GATE_INITIAL_SECONDS_SLOW = _env_float("SUNARP_GATE_INITIAL_SECONDS_SLOW", 20.0)
GATE_RETRY_SECONDS_SLOW = _env_float("SUNARP_GATE_RETRY_SECONDS_SLOW", 12.0)

API_WAIT_SECONDS = _env_float("SUNARP_API_WAIT_SECONDS", 6.0)
API_WAIT_SECONDS_SLOW = _env_float("SUNARP_API_WAIT_SECONDS_SLOW", 16.0)

NO_PROGRESS_FAIL_SECONDS = _env_float("SUNARP_NO_PROGRESS_FAIL_SECONDS", 3.5)
NO_PROGRESS_FAIL_SECONDS_SLOW = _env_float("SUNARP_NO_PROGRESS_FAIL_SECONDS_SLOW", 8.0)

PROGRESS_EXTENSION_SECONDS = _env_float("SUNARP_PROGRESS_EXTENSION_SECONDS", 6.0)
MAX_PROGRESS_EXTENSIONS = _env_int("SUNARP_MAX_PROGRESS_EXTENSIONS", 2)
MAX_PROGRESS_EXTENSIONS_SLOW = _env_int("SUNARP_MAX_PROGRESS_EXTENSIONS_SLOW", 4)

MAX_SUBMIT_ATTEMPTS = _env_int("SUNARP_MAX_SUBMIT_ATTEMPTS", 2)
MAX_SUBMIT_ATTEMPTS_SLOW = _env_int("SUNARP_MAX_SUBMIT_ATTEMPTS_SLOW", 3)

REQUIRE_TOKEN_ON_RETRY = os.getenv("SUNARP_REQUIRE_TOKEN_ON_RETRY", "true").lower() == "true"


@dataclass
class SedeInfo:
    """Information about a SUNARP office/sede."""
    prefijo: str = ""
    reg_pub_id: str = ""
    ofic_reg_id: str = ""
    nombre: str = ""
    fg_baja: str = ""
    placa: str = ""
    num_partida: str = ""
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SedeInfo":
        return cls(
            prefijo=data.get("prefijo", ""),
            reg_pub_id=data.get("regPubId", ""),
            ofic_reg_id=data.get("oficRegId", ""),
            nombre=data.get("nombre", ""),
            fg_baja=data.get("fgBaja", ""),
            placa=data.get("placa", "").strip(),
            num_partida=data.get("numPartida", ""),
        )


@dataclass
class VehicleOCRData:
    """Extracted vehicle data from OCR."""
    placa: Optional[str] = None
    serie: Optional[str] = None
    vin: Optional[str] = None
    motor: Optional[str] = None
    color: Optional[str] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    placa_vigente: Optional[str] = None
    placa_anterior: Optional[str] = None
    estado: Optional[str] = None
    anotaciones: Optional[str] = None
    sede: Optional[str] = None
    anio_modelo: Optional[str] = None
    propietario: Optional[str] = None
    raw_text: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VehicleOCRData":
        return cls(
            placa=data.get("placa"),
            serie=data.get("serie"),
            vin=data.get("vin"),
            motor=data.get("motor"),
            color=data.get("color"),
            marca=data.get("marca"),
            modelo=data.get("modelo"),
            placa_vigente=data.get("placa_vigente"),
            placa_anterior=data.get("placa_anterior"),
            estado=data.get("estado"),
            anotaciones=data.get("anotaciones"),
            sede=data.get("sede"),
            anio_modelo=data.get("anio_modelo"),
            propietario=data.get("propietario"),
            raw_text=data.get("raw_text"),
        )
    
    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "placa": self.placa,
            "serie": self.serie,
            "vin": self.vin,
            "motor": self.motor,
            "color": self.color,
            "marca": self.marca,
            "modelo": self.modelo,
            "placa_vigente": self.placa_vigente,
            "placa_anterior": self.placa_anterior,
            "estado": self.estado,
            "anotaciones": self.anotaciones,
            "sede": self.sede,
            "anio_modelo": self.anio_modelo,
            "propietario": self.propietario,
        }
        if include_raw:
            result["raw_text"] = self.raw_text
        return result


@dataclass
class ConsultaResult:
    """Result of a SUNARP vehicle consultation."""
    success: bool = False
    image_path: Optional[str] = None
    placa: str = ""
    cod: int = 0
    mensaje: str = ""
    mensaje_txt: str = ""
    icon: str = ""
    alerta_robo: str = ""
    sedes: List[SedeInfo] = field(default_factory=list)
    raw_response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # OCR extracted data
    ocr_data: Optional[VehicleOCRData] = None
    ocr_error: Optional[str] = None
    attempts: int = 0
    state_timeline: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self, include_ocr_raw: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "success": self.success,
            "image_path": self.image_path,
            "placa": self.placa,
            "cod": self.cod,
            "mensaje": self.mensaje,
            "mensaje_txt": self.mensaje_txt,
            "icon": self.icon,
            "alerta_robo": self.alerta_robo,
            "sedes": [
                {
                    "prefijo": s.prefijo,
                    "regPubId": s.reg_pub_id,
                    "oficRegId": s.ofic_reg_id,
                    "nombre": s.nombre,
                    "fgBaja": s.fg_baja,
                    "placa": s.placa,
                    "numPartida": s.num_partida,
                }
                for s in self.sedes
            ],
            "error": self.error,
            "attempts": self.attempts,
        }
        
        # Add OCR data if available
        if self.ocr_data:
            result["vehiculo"] = self.ocr_data.to_dict(include_raw=include_ocr_raw)
        else:
            result["vehiculo"] = None
        
        if self.ocr_error:
            result["ocr_error"] = self.ocr_error

        if self.state_timeline:
            result["state_timeline"] = self.state_timeline
        
        return result


class SunarpScraper:
    """Scraper for SUNARP vehicle consultation page with API interception."""
    
    def __init__(self, headless: bool = False, slow_mode: bool = False):
        """
        Initialize the scraper.
        
        Args:
            headless: If True, run browser without visible window.
                      Note: Headless mode may fail with Cloudflare Turnstile.
            slow_mode: If True, use longer delays for slow internet connections.
        """
        self.headless = headless
        self.slow_mode = slow_mode
        self.downloads_dir = DOWNLOADS_DIR
        self.downloads_dir.mkdir(exist_ok=True)
        self._api_response: Optional[Dict[str, Any]] = None
        self._response_event = asyncio.Event()
        self._pending_request_id: Optional[str] = None
        self._tab = None
        self._state_timeline: List[Dict[str, Any]] = []

    def _record_state(self, phase: str, **details: Any):
        """Record internal state transitions for debugging and observability."""
        entry = {
            "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "phase": phase,
        }
        if details:
            entry["details"] = details
        self._state_timeline.append(entry)
        # Keep a bounded timeline to avoid unbounded growth
        if len(self._state_timeline) > 200:
            self._state_timeline = self._state_timeline[-200:]

    def _extract_script_value(self, result: Any) -> Any:
        """Extract the actual value from pydoll execute_script responses."""
        if isinstance(result, dict):
            if "result" in result:
                inner = result["result"]
                if isinstance(inner, dict) and "result" in inner:
                    inner = inner["result"]
                if isinstance(inner, dict) and "value" in inner:
                    return inner["value"]
                return inner
        return result

    def _get_timing_profile(self) -> Dict[str, Any]:
        """Get validated timing profile for current mode."""
        if self.slow_mode:
            total_budget_seconds = _positive_float(TOTAL_BUDGET_SLOW_SECONDS, 75.0)
            gate_initial_seconds = _positive_float(GATE_INITIAL_SECONDS_SLOW, 20.0)
            gate_retry_seconds = _positive_float(GATE_RETRY_SECONDS_SLOW, 12.0)
            api_wait_seconds = _positive_float(API_WAIT_SECONDS_SLOW, 16.0)
            no_progress_fail_seconds = _positive_float(NO_PROGRESS_FAIL_SECONDS_SLOW, 8.0)
            max_progress_extensions = _positive_int(MAX_PROGRESS_EXTENSIONS_SLOW, 4, minimum=0)
            max_submit_attempts = _positive_int(MAX_SUBMIT_ATTEMPTS_SLOW, 3)
        else:
            total_budget_seconds = _positive_float(TOTAL_BUDGET_SECONDS, 18.0)
            gate_initial_seconds = _positive_float(GATE_INITIAL_SECONDS, 8.0)
            gate_retry_seconds = _positive_float(GATE_RETRY_SECONDS, 4.0)
            api_wait_seconds = _positive_float(API_WAIT_SECONDS, 6.0)
            no_progress_fail_seconds = _positive_float(NO_PROGRESS_FAIL_SECONDS, 3.5)
            max_progress_extensions = _positive_int(MAX_PROGRESS_EXTENSIONS, 2, minimum=0)
            max_submit_attempts = _positive_int(MAX_SUBMIT_ATTEMPTS, 2)

        # Keep reasonable floor to avoid immediate timeout loops
        progress_extension_seconds = _positive_float(PROGRESS_EXTENSION_SECONDS, 6.0)
        fast_target_seconds = _positive_float(FAST_TARGET_SECONDS, 12.0)

        return {
            "fast_target_seconds": fast_target_seconds,
            "total_budget_seconds": total_budget_seconds,
            "gate_initial_seconds": gate_initial_seconds,
            "gate_retry_seconds": gate_retry_seconds,
            "api_wait_seconds": api_wait_seconds,
            "no_progress_fail_seconds": no_progress_fail_seconds,
            "progress_extension_seconds": progress_extension_seconds,
            "max_progress_extensions": max_progress_extensions,
            "max_submit_attempts": max_submit_attempts,
            "require_token_on_retry": bool(REQUIRE_TOKEN_ON_RETRY),
        }

    async def _run_json_script(self, tab, script: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute script and parse JSON result safely."""
        try:
            raw = await tab.execute_script(script)
            value = self._extract_script_value(raw)
            if isinstance(value, str):
                return json.loads(value)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        return default or {}

    async def _inject_runtime_watchers(self, tab):
        """Inject runtime observers for dynamic, response-driven flow control."""
        watcher_script = """
        (function() {
            const API_HOST = 'api-gateway.sunarp.gob.pe';
            const API_PATH = 'getDatosVehiculo';

            if (!window.__sunarpRuntime) {
                window.__sunarpRuntime = {
                    initialized: false,
                    apiRequestCount: 0,
                    apiRequestInFlight: false,
                    apiLastRequestAt: 0,
                    apiLastResponseAt: 0,
                    apiLastError: '',
                    submitClicks: 0,
                    lastSubmitAt: 0,
                    events: []
                };
            }

            const runtime = window.__sunarpRuntime;

            const pushEvent = (type, data) => {
                try {
                    runtime.events.push({
                        type,
                        at: Date.now(),
                        ...(data || {})
                    });
                    if (runtime.events.length > 150) {
                        runtime.events = runtime.events.slice(-150);
                    }
                } catch (e) {}
            };

            runtime.pushEvent = pushEvent;

            if (!runtime.fetchPatched && window.fetch) {
                const originalFetch = window.fetch;
                window.fetch = function() {
                    const input = arguments[0];
                    const url = (typeof input === 'string') ? input : ((input && input.url) ? input.url : '');
                    const isApi = url.includes(API_HOST) && url.includes(API_PATH);

                    if (isApi) {
                        runtime.apiRequestCount += 1;
                        runtime.apiRequestInFlight = true;
                        runtime.apiLastRequestAt = Date.now();
                        pushEvent('api_fetch_request', { url });
                    }

                    return originalFetch.apply(this, arguments)
                        .then((response) => {
                            if (isApi) {
                                runtime.apiRequestInFlight = false;
                                runtime.apiLastResponseAt = Date.now();
                                pushEvent('api_fetch_response', { status: response.status });
                            }
                            return response;
                        })
                        .catch((error) => {
                            if (isApi) {
                                runtime.apiRequestInFlight = false;
                                runtime.apiLastError = String(error);
                                pushEvent('api_fetch_error', { error: String(error) });
                            }
                            throw error;
                        });
                };
                runtime.fetchPatched = true;
            }

            if (!runtime.xhrPatched && window.XMLHttpRequest) {
                const originalOpen = XMLHttpRequest.prototype.open;
                const originalSend = XMLHttpRequest.prototype.send;

                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__sunarpUrl = url || '';
                    this.__sunarpMethod = method || '';
                    return originalOpen.apply(this, arguments);
                };

                XMLHttpRequest.prototype.send = function() {
                    const url = this.__sunarpUrl || '';
                    const isApi = url.includes(API_HOST) && url.includes(API_PATH);

                    if (isApi) {
                        runtime.apiRequestCount += 1;
                        runtime.apiRequestInFlight = true;
                        runtime.apiLastRequestAt = Date.now();
                        pushEvent('api_xhr_request', { url, method: this.__sunarpMethod || '' });

                        this.addEventListener('loadend', function() {
                            runtime.apiRequestInFlight = false;
                            runtime.apiLastResponseAt = Date.now();
                            pushEvent('api_xhr_loadend', { status: this.status });
                        });

                        this.addEventListener('error', function() {
                            runtime.apiRequestInFlight = false;
                            runtime.apiLastError = 'xhr_error';
                            pushEvent('api_xhr_error', {});
                        });

                        this.addEventListener('abort', function() {
                            runtime.apiRequestInFlight = false;
                            runtime.apiLastError = 'xhr_abort';
                            pushEvent('api_xhr_abort', {});
                        });
                    }

                    return originalSend.apply(this, arguments);
                };

                runtime.xhrPatched = true;
            }

            runtime.initialized = true;
            return JSON.stringify({ ok: true, initialized: runtime.initialized });
        })();
        """
        await self._run_json_script(tab, watcher_script, default={"ok": False})

    async def _get_runtime_state(self, tab) -> Dict[str, Any]:
        """Read dynamic page/runtime state used by the state machine."""
        state_script = """
        (function() {
            const plateInput = document.querySelector("input#placa, input[name='placa'], input[formcontrolname='placa'], input[placeholder*='ABC'], input[type='text']");
            const searchBtn = document.querySelector('button.btn-sunarp-green, button.ant-btn-primary');
            const turnstileFrame = document.querySelector('iframe[src*="turnstile"]');
            const tokenInput = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');

            const tokenLength = tokenInput && tokenInput.value ? tokenInput.value.length : 0;
            const hasToken = tokenLength > 20;

            const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').toLowerCase();
            const captchaError = bodyText.includes('resuelva el captcha') || bodyText.includes('captcha no resuelto');

            const runtime = window.__sunarpRuntime || {};
            const events = Array.isArray(runtime.events) ? runtime.events : [];
            const lastEvent = events.length ? events[events.length - 1] : null;

            return JSON.stringify({
                hasPlateInput: !!plateInput,
                plateValue: plateInput && plateInput.value ? plateInput.value : '',
                buttonFound: !!searchBtn,
                buttonEnabled: !!searchBtn && !searchBtn.disabled,
                hasTurnstile: !!turnstileFrame,
                hasToken,
                tokenLength,
                captchaError,
                apiRequestCount: runtime.apiRequestCount || 0,
                apiRequestInFlight: !!runtime.apiRequestInFlight,
                apiLastRequestAt: runtime.apiLastRequestAt || 0,
                apiLastResponseAt: runtime.apiLastResponseAt || 0,
                apiLastError: runtime.apiLastError || '',
                submitClicks: runtime.submitClicks || 0,
                lastSubmitAt: runtime.lastSubmitAt || 0,
                eventCount: events.length,
                lastEventType: lastEvent ? (lastEvent.type || '') : ''
            });
        })();
        """

        return await self._run_json_script(
            tab,
            state_script,
            default={
                "hasPlateInput": False,
                "plateValue": "",
                "buttonFound": False,
                "buttonEnabled": False,
                "hasTurnstile": False,
                "hasToken": False,
                "tokenLength": 0,
                "captchaError": False,
                "apiRequestCount": 0,
                "apiRequestInFlight": False,
                "apiLastRequestAt": 0,
                "apiLastResponseAt": 0,
                "apiLastError": "",
                "submitClicks": 0,
                "lastSubmitAt": 0,
                "eventCount": 0,
                "lastEventType": "",
            },
        )

    def _build_failure_result(self, placa: str, error: str, attempts: int = 0) -> ConsultaResult:
        """Build a standardized failed result with state timeline attached."""
        return ConsultaResult(
            success=False,
            placa=placa,
            error=error,
            attempts=attempts,
            state_timeline=list(self._state_timeline),
        )
    
    def _get_browser_options(self) -> ChromiumOptions:
        """Configure Chrome options to appear as genuine Google Chrome."""
        options = ChromiumOptions()
        
        # Set browser path (supports local and Docker paths)
        for browser_path in CHROMIUM_CANDIDATE_PATHS:
            if browser_path and os.path.exists(browser_path):
                options.binary_location = browser_path
                break
        
        # === CRITICAL: Disable automation detection ===
        options.add_argument("--disable-blink-features=AutomationControlled")
        
        # === Window and display settings ===
        options.add_argument("--window-size=1366,768")
        options.add_argument("--start-maximized")
        
        # === User-Agent: Use exact Chrome stable user-agent ===
        # This matches a real Chrome 131 on Linux
        chrome_version = "131.0.0.0"
        user_agent = (
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
        )
        options.add_argument(f"--user-agent={user_agent}")
        
        # === Language and locale ===
        options.add_argument("--lang=es-419")  # Latin American Spanish
        options.add_argument("--accept-lang=es-419,es;q=0.9,en;q=0.8")
        
        # === Disable features that reveal automation ===
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")
        
        # === Enable features that real Chrome has ===
        options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
        
        # === GPU and rendering (match real Chrome) ===
        options.add_argument("--enable-gpu-rasterization")
        options.add_argument("--enable-zero-copy")
        options.add_argument("--ignore-gpu-blocklist")
        
        # === Disable features that might fingerprint as Chromium ===
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-client-side-phishing-detection")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-hang-monitor")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-prompt-on-repost")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--safebrowsing-disable-auto-update")
        
        # === WebRTC: Prevent IP leak but keep functional ===
        options.add_argument("--disable-webrtc-hw-encoding")
        options.add_argument("--disable-webrtc-hw-decoding")
        
        # Block notifications
        options.block_notifications = True
        
        # === Headless mode configuration ===
        if self.headless:
            # Use the "new" headless mode which is harder to detect
            # --headless=new is available in Chrome 109+
            options.add_argument("--headless=new")
            
            # Additional headless-specific anti-detection
            options.add_argument("--disable-gpu")  # Recommended for headless
            
            # Disable headless-specific features that can be detected
            options.add_argument("--disable-extensions")
            
            print("[WARN] Running in headless mode - Cloudflare Turnstile may not complete!")
            print("[WARN] If captcha fails, try running with headless=False")
        
        return options
    
    async def _on_frame_navigated(self, event: Dict[str, Any]):
        """Callback for frame navigation - re-inject anti-detection scripts."""
        try:
            # Re-inject anti-detection on each navigation
            if self._tab:
                await self._inject_anti_detection(self._tab)
                await self._inject_runtime_watchers(self._tab)
        except Exception as e:
            print(f"[DEBUG] Frame navigation callback error: {e}")
    
    async def consultar_placa(self, placa: str) -> ConsultaResult:
        """
        Query a vehicle plate number on SUNARP.
        
        Args:
            placa: The vehicle plate number to query (e.g., "ABC123").
            
        Returns:
            ConsultaResult with image path and metadata.
        """
        placa = placa.strip().upper()
        self._state_timeline = []
        self._record_state("consulta_start", placa=placa, slow_mode=self.slow_mode, headless=self.headless)
        self._api_response = None
        self._response_event.clear()
        self._pending_request_id = None
        
        options = self._get_browser_options()
        
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            self._tab = tab
            
            # Enable page events to track navigation
            print(f"[INFO] Enabling page events...")
            await tab.enable_page_events()
            
            # Register callback to re-inject scripts on each page load
            await tab.on(PageEvent.FRAME_NAVIGATED, self._on_frame_navigated)
            await tab.on(PageEvent.LOAD_EVENT_FIRED, self._on_frame_navigated)
            
            # Inject anti-detection BEFORE any navigation
            print(f"[INFO] Injecting anti-detection scripts...")
            await self._inject_anti_detection(tab)
            await self._inject_runtime_watchers(tab)
            
            # Enable network events to intercept API responses
            print(f"[INFO] Enabling network interception...")
            await tab.enable_network_events()
            
            # Register callback for network responses
            await tab.on(NetworkEvent.RESPONSE_RECEIVED, self._on_response_received)
            
            # Navigate to the base domain first (helps with session/cookies)
            print(f"[INFO] Navigating to base domain...")
            await tab.go_to("https://consultavehicular.sunarp.gob.pe/")
            self._record_state("navigated_base_domain")
            
            print(f"[INFO] Waiting for page elements to load...")
            cf_timeout = CLOUDFLARE_TIMEOUT * (1.5 if self.slow_mode else 1.0)
            try:
                await self._wait_for_cloudflare(tab, timeout=int(cf_timeout))
            except TimeoutError:
                return self._build_failure_result(
                    placa,
                    "Cloudflare/Turnstile no completado en tiempo esperado",
                )
            
            print(f"[INFO] Filling plate number: {placa}")
            fill_ok = await self._fill_plate_input(tab, placa)
            if not fill_ok:
                return self._build_failure_result(placa, "No se pudo completar el campo placa")

            plate_ok = await self._wait_for_plate_value(tab, placa, timeout=25)
            if not plate_ok:
                return self._build_failure_result(placa, "La placa no quedó registrada en el formulario")

            timing = self._get_timing_profile()
            loop = asyncio.get_event_loop()
            request_window_start = loop.time()
            request_deadline = request_window_start + timing["total_budget_seconds"]

            self._record_state(
                "timing_profile",
                fast_target_seconds=timing["fast_target_seconds"],
                total_budget_seconds=timing["total_budget_seconds"],
                gate_initial_seconds=timing["gate_initial_seconds"],
                gate_retry_seconds=timing["gate_retry_seconds"],
                api_wait_seconds=timing["api_wait_seconds"],
                no_progress_fail_seconds=timing["no_progress_fail_seconds"],
                progress_extension_seconds=timing["progress_extension_seconds"],
                max_progress_extensions=timing["max_progress_extensions"],
                max_submit_attempts=timing["max_submit_attempts"],
                require_token_on_retry=timing["require_token_on_retry"],
            )

            max_submit_attempts = timing["max_submit_attempts"]
            attempts_used = 0
            last_error = "No API response received"
            saw_turnstile = False
            saw_token = False
            remaining_progress_extensions = timing["max_progress_extensions"]

            for attempt in range(1, max_submit_attempts + 1):
                attempts_used = attempt
                remaining_budget = request_deadline - loop.time()
                if remaining_budget <= 0:
                    last_error = "SUNARP no envió solicitud al API (presupuesto agotado)."
                    self._record_state("request_budget_exhausted", attempt=attempt)
                    break

                gate_window = (
                    timing["gate_initial_seconds"]
                    if attempt == 1
                    else timing["gate_retry_seconds"]
                )
                gate_timeout = min(remaining_budget, gate_window)
                require_token = bool(
                    attempt > 1
                    and timing["require_token_on_retry"]
                    and (
                        saw_turnstile
                        or saw_token
                        or "captcha" in (last_error or "").lower()
                    )
                )

                self._record_state(
                    "submit_attempt_start",
                    attempt=attempt,
                    remaining_budget=round(remaining_budget, 2),
                    gate_timeout=round(gate_timeout, 2),
                    require_token=require_token,
                    remaining_progress_extensions=remaining_progress_extensions,
                )

                # Reset response tracking for this attempt
                self._api_response = None
                self._response_event.clear()
                self._pending_request_id = None

                gate_state = await self._wait_for_submission_gate(
                    tab,
                    attempt=attempt,
                    timeout_seconds=gate_timeout,
                    require_token=require_token,
                )
                saw_turnstile = saw_turnstile or bool(gate_state.get("hasTurnstile"))
                saw_token = saw_token or bool(gate_state.get("hasToken"))

                if not gate_state.get("ready"):
                    last_error = gate_state.get("error", "Captcha no resuelto")
                    self._record_state("submit_gate_failed", attempt=attempt, error=last_error)
                    if attempt < max_submit_attempts:
                        continue
                    return self._build_failure_result(placa, last_error, attempts=attempts_used)

                self._record_state(
                    "submit_gate_ready",
                    attempt=attempt,
                    gate_reason=gate_state.get("gate_reason", "unknown"),
                    has_token=gate_state.get("hasToken", False),
                    has_turnstile=gate_state.get("hasTurnstile", False),
                )

                clicked = await self._click_search_button(tab)
                if not clicked:
                    last_error = "No se pudo hacer click en el botón de búsqueda"
                    self._record_state("submit_click_failed", attempt=attempt)
                    if attempt < max_submit_attempts:
                        continue
                    return self._build_failure_result(placa, last_error, attempts=attempts_used)

                remaining_budget_after_click = request_deadline - loop.time()
                if remaining_budget_after_click <= 0:
                    last_error = "SUNARP no envió solicitud al API (presupuesto agotado)."
                    self._record_state(
                        "request_budget_exhausted",
                        attempt=attempt,
                        stage="after_submit_click",
                    )
                    break

                initial_api_wait = min(
                    timing["api_wait_seconds"],
                    remaining_budget_after_click,
                )
                print(
                    f"[INFO] Waiting for API response (attempt {attempt}/{max_submit_attempts}, "
                    f"window={initial_api_wait:.1f}s, budget_left={remaining_budget_after_click:.1f}s)..."
                )

                outcome = await self._wait_for_api_response(
                    tab,
                    attempt=attempt,
                    baseline_state=gate_state,
                    initial_wait_seconds=initial_api_wait,
                    total_deadline=request_deadline,
                    no_progress_fail_seconds=timing["no_progress_fail_seconds"],
                    progress_extension_seconds=timing["progress_extension_seconds"],
                    max_progress_extensions=remaining_progress_extensions,
                )

                extensions_used = int(outcome.get("extensions_used", 0) or 0)
                if extensions_used > 0:
                    remaining_progress_extensions = max(
                        0,
                        remaining_progress_extensions - extensions_used,
                    )

                if outcome.get("status") == "response_ready" and self._api_response:
                    self._record_state("submit_attempt_success", attempt=attempt)
                    break

                last_error = outcome.get("error", "No API response received")
                self._record_state(
                    "submit_attempt_failed",
                    attempt=attempt,
                    status=outcome.get("status", "unknown"),
                    error=last_error,
                )

                if attempt < max_submit_attempts:
                    if (request_deadline - loop.time()) <= 0:
                        self._record_state("request_budget_exhausted", attempt=attempt, stage="before_retry")
                        break
                    # Re-ensure plate value before retrying
                    await self._wait_for_plate_value(tab, placa, timeout=min(10, max(1.0, request_deadline - loop.time())))
                    continue

            # Process final outcome
            if not self._api_response:
                return self._build_failure_result(placa, last_error, attempts=attempts_used)

            print(f"[INFO] Processing API response...")
            processed = await self._process_api_response(self._api_response, placa)
            processed.attempts = attempts_used
            processed.state_timeline = list(self._state_timeline)
            return processed
    
    async def _inject_anti_detection(self, tab):
        """Inject comprehensive JavaScript to make Chromium appear as genuine Chrome."""
        
        # Complete Chrome spoofing script
        stealth_script = """
        (function() {
            'use strict';
            
            // === 1. Hide webdriver property ===
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });
            
            // Delete webdriver from navigator prototype
            delete Navigator.prototype.webdriver;
            
            // === 2. Spoof Chrome runtime and app ===
            window.chrome = {
                app: {
                    isInstalled: false,
                    InstallState: {
                        DISABLED: 'disabled',
                        INSTALLED: 'installed',
                        NOT_INSTALLED: 'not_installed'
                    },
                    RunningState: {
                        CANNOT_RUN: 'cannot_run',
                        READY_TO_RUN: 'ready_to_run',
                        RUNNING: 'running'
                    },
                    getDetails: function() { return null; },
                    getIsInstalled: function() { return false; },
                    installState: function(callback) { 
                        callback && callback('not_installed'); 
                    },
                    runningState: function() { return 'cannot_run'; }
                },
                runtime: {
                    OnInstalledReason: {
                        CHROME_UPDATE: 'chrome_update',
                        INSTALL: 'install',
                        SHARED_MODULE_UPDATE: 'shared_module_update',
                        UPDATE: 'update'
                    },
                    OnRestartRequiredReason: {
                        APP_UPDATE: 'app_update',
                        OS_UPDATE: 'os_update',
                        PERIODIC: 'periodic'
                    },
                    PlatformArch: {
                        ARM: 'arm',
                        ARM64: 'arm64',
                        MIPS: 'mips',
                        MIPS64: 'mips64',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64'
                    },
                    PlatformNaclArch: {
                        ARM: 'arm',
                        MIPS: 'mips',
                        MIPS64: 'mips64',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64'
                    },
                    PlatformOs: {
                        ANDROID: 'android',
                        CROS: 'cros',
                        LINUX: 'linux',
                        MAC: 'mac',
                        OPENBSD: 'openbsd',
                        WIN: 'win'
                    },
                    RequestUpdateCheckStatus: {
                        NO_UPDATE: 'no_update',
                        THROTTLED: 'throttled',
                        UPDATE_AVAILABLE: 'update_available'
                    },
                    connect: function() { return null; },
                    sendMessage: function() { return null; },
                    id: undefined
                },
                csi: function() { return null; },
                loadTimes: function() {
                    return {
                        commitLoadTime: Date.now() / 1000 - Math.random() * 2,
                        connectionInfo: 'h2',
                        finishDocumentLoadTime: Date.now() / 1000 - Math.random(),
                        finishLoadTime: Date.now() / 1000 - Math.random() * 0.5,
                        firstPaintAfterLoadTime: 0,
                        firstPaintTime: Date.now() / 1000 - Math.random() * 1.5,
                        navigationType: 'Other',
                        npnNegotiatedProtocol: 'h2',
                        requestTime: Date.now() / 1000 - Math.random() * 3,
                        startLoadTime: Date.now() / 1000 - Math.random() * 2.5,
                        wasAlternateProtocolAvailable: false,
                        wasFetchedViaSpdy: true,
                        wasNpnNegotiated: true
                    };
                }
            };
            
            // === 3. Spoof plugins (Chrome has these by default) ===
            const makePluginArray = () => {
                const plugins = [
                    {
                        name: 'Chrome PDF Plugin',
                        description: 'Portable Document Format',
                        filename: 'internal-pdf-viewer',
                        length: 1
                    },
                    {
                        name: 'Chrome PDF Viewer',
                        description: '',
                        filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                        length: 1
                    },
                    {
                        name: 'Native Client',
                        description: '',
                        filename: 'internal-nacl-plugin',
                        length: 2
                    }
                ];
                
                const pluginArray = Object.create(PluginArray.prototype);
                plugins.forEach((p, i) => {
                    const plugin = Object.create(Plugin.prototype);
                    Object.defineProperties(plugin, {
                        name: { value: p.name, enumerable: true },
                        description: { value: p.description, enumerable: true },
                        filename: { value: p.filename, enumerable: true },
                        length: { value: p.length, enumerable: true }
                    });
                    pluginArray[i] = plugin;
                    pluginArray[p.name] = plugin;
                });
                
                Object.defineProperty(pluginArray, 'length', { value: plugins.length });
                pluginArray.item = function(index) { return this[index] || null; };
                pluginArray.namedItem = function(name) { return this[name] || null; };
                pluginArray.refresh = function() {};
                
                return pluginArray;
            };
            
            Object.defineProperty(navigator, 'plugins', {
                get: () => makePluginArray(),
                configurable: true
            });
            
            // === 4. Spoof mimeTypes ===
            const makeMimeTypeArray = () => {
                const mimeTypes = [
                    { type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf' },
                    { type: 'application/x-google-chrome-pdf', description: 'Portable Document Format', suffixes: 'pdf' },
                    { type: 'application/x-nacl', description: 'Native Client Executable', suffixes: '' },
                    { type: 'application/x-pnacl', description: 'Portable Native Client Executable', suffixes: '' }
                ];
                
                const mimeTypeArray = Object.create(MimeTypeArray.prototype);
                mimeTypes.forEach((m, i) => {
                    const mimeType = Object.create(MimeType.prototype);
                    Object.defineProperties(mimeType, {
                        type: { value: m.type, enumerable: true },
                        description: { value: m.description, enumerable: true },
                        suffixes: { value: m.suffixes, enumerable: true },
                        enabledPlugin: { value: navigator.plugins[0], enumerable: true }
                    });
                    mimeTypeArray[i] = mimeType;
                    mimeTypeArray[m.type] = mimeType;
                });
                
                Object.defineProperty(mimeTypeArray, 'length', { value: mimeTypes.length });
                mimeTypeArray.item = function(index) { return this[index] || null; };
                mimeTypeArray.namedItem = function(name) { return this[name] || null; };
                
                return mimeTypeArray;
            };
            
            Object.defineProperty(navigator, 'mimeTypes', {
                get: () => makeMimeTypeArray(),
                configurable: true
            });
            
            // === 5. Spoof languages ===
            Object.defineProperty(navigator, 'languages', {
                get: () => ['es-419', 'es', 'en-US', 'en'],
                configurable: true
            });
            
            Object.defineProperty(navigator, 'language', {
                get: () => 'es-419',
                configurable: true
            });
            
            // === 6. Spoof platform ===
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Linux x86_64',
                configurable: true
            });
            
            // === 7. Spoof vendor (Chrome-specific) ===
            Object.defineProperty(navigator, 'vendor', {
                get: () => 'Google Inc.',
                configurable: true
            });
            
            // === 8. Spoof product sub ===
            Object.defineProperty(navigator, 'productSub', {
                get: () => '20030107',
                configurable: true
            });
            
            // === 9. Spoof hardwareConcurrency (realistic value) ===
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
                configurable: true
            });
            
            // === 10. Spoof deviceMemory ===
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true
            });
            
            // === 11. Spoof maxTouchPoints ===
            Object.defineProperty(navigator, 'maxTouchPoints', {
                get: () => 0,
                configurable: true
            });
            
            // === 12. Spoof connection (NetworkInformation API) ===
            if (navigator.connection) {
                Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
                Object.defineProperty(navigator.connection, 'downlink', { get: () => 10 });
                Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
                Object.defineProperty(navigator.connection, 'saveData', { get: () => false });
            }
            
            // === 13. Fix permissions API ===
            const originalQuery = navigator.permissions.query;
            navigator.permissions.query = function(parameters) {
                if (parameters.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission });
                }
                return originalQuery.call(this, parameters);
            };
            
            // === 14. Spoof WebGL vendor and renderer ===
            const getParameterProxyHandler = {
                apply: function(target, thisArg, argumentsList) {
                    const param = argumentsList[0];
                    const gl = thisArg;
                    
                    // UNMASKED_VENDOR_WEBGL
                    if (param === 37445) {
                        return 'Google Inc. (Intel)';
                    }
                    // UNMASKED_RENDERER_WEBGL  
                    if (param === 37446) {
                        return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)';
                    }
                    
                    return Reflect.apply(target, thisArg, argumentsList);
                }
            };
            
            // Apply to WebGL
            const getContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, attributes) {
                const context = getContext.call(this, type, attributes);
                if (context && (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl')) {
                    const originalGetParameter = context.getParameter.bind(context);
                    context.getParameter = new Proxy(originalGetParameter, getParameterProxyHandler);
                }
                return context;
            };
            
            // === 15. Spoof Notification permission state ===
            if (typeof Notification !== 'undefined') {
                Object.defineProperty(Notification, 'permission', {
                    get: () => 'default',
                    configurable: true
                });
            }
            
            // === 16. Remove Chromium-specific properties ===
            // Some sites check for these being undefined in real Chrome
            try {
                delete window.domAutomation;
                delete window.domAutomationController;
            } catch(e) {}
            
            // === 17. Spoof screen properties ===
            Object.defineProperty(screen, 'colorDepth', { get: () => 24, configurable: true });
            Object.defineProperty(screen, 'pixelDepth', { get: () => 24, configurable: true });
            
            // === 18. Fix iframe contentWindow ===
            const originalContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function() {
                    const iframe = originalContentWindow.get.call(this);
                    if (iframe) {
                        try {
                            Object.defineProperty(iframe.navigator, 'webdriver', {
                                get: () => undefined,
                                configurable: true
                            });
                        } catch(e) {}
                    }
                    return iframe;
                }
            });
            
            console.log('[Stealth] Anti-detection measures applied');
        })();
        """
        
        try:
            await tab.execute_script(stealth_script)
            print("[INFO] Anti-detection scripts injected successfully")
        except Exception as e:
            print(f"[WARN] Failed to inject some anti-detection scripts: {e}")
    
    async def _on_response_received(self, event: Dict[str, Any]):
        """Callback for network response events."""
        try:
            params = event.get("params", {})
            response = params.get("response", {})
            url = response.get("url", "")
            
            # Check if this is the SUNARP API response
            if SUNARP_API_URL in url and SUNARP_API_ENDPOINT in url:
                request_id = params.get("requestId")
                if request_id:
                    # Store the request ID - we'll get the body later
                    self._pending_request_id = request_id
                    self._response_event.set()
                    print(f"[INFO] Intercepted API response: {url[:80]}...")
        except Exception as e:
            print(f"[DEBUG] Error in response callback: {e}")
    
    async def _wait_for_api_response(
        self,
        tab,
        attempt: int,
        baseline_state: Dict[str, Any],
        initial_wait_seconds: float,
        total_deadline: float,
        no_progress_fail_seconds: float,
        progress_extension_seconds: float,
        max_progress_extensions: int,
    ) -> Dict[str, Any]:
        """Wait for API response with progress-aware extensions and global budget cap."""
        loop = asyncio.get_event_loop()
        poll_interval = 0.45 if self.slow_mode else 0.22
        started_at = loop.time()

        initial_wait = _positive_float(initial_wait_seconds, 2.0)
        no_progress_fail = _positive_float(no_progress_fail_seconds, 3.5)
        extension_size = _positive_float(progress_extension_seconds, 4.0)
        extensions_allowed = _positive_int(max_progress_extensions, 0, minimum=0)

        active_deadline = min(total_deadline, started_at + initial_wait)

        observed_api_count = int(baseline_state.get("apiRequestCount", 0) or 0)
        observed_submit_clicks = int(baseline_state.get("submitClicks", 0) or 0)
        observed_event_count = int(baseline_state.get("eventCount", 0) or 0)
        observed_last_response_at = int(baseline_state.get("apiLastResponseAt", 0) or 0)

        request_seen = False
        last_progress_at = started_at
        extensions_used = 0
        captcha_error_hits = 0

        self._record_state(
            "api_wait_start",
            attempt=attempt,
            initial_wait_seconds=round(initial_wait, 2),
            no_progress_fail_seconds=round(no_progress_fail, 2),
            extensions_allowed=extensions_allowed,
            extension_size_seconds=round(extension_size, 2),
        )

        while True:
            now = loop.time()
            if now >= total_deadline:
                self._record_state("request_budget_exhausted", attempt=attempt, stage="api_wait")
                if request_seen:
                    return {
                        "status": "api_timeout",
                        "error": "Timeout waiting for API response from SUNARP.",
                        "extensions_used": extensions_used,
                    }
                return {
                    "status": "submission_timeout",
                    "error": "No se detectó envío de solicitud al API SUNARP.",
                    "extensions_used": extensions_used,
                }

            if now >= active_deadline:
                if request_seen:
                    return {
                        "status": "api_timeout",
                        "error": "Timeout waiting for API response from SUNARP.",
                        "extensions_used": extensions_used,
                    }
                return {
                    "status": "submission_timeout",
                    "error": "No se detectó envío de solicitud al API SUNARP.",
                    "extensions_used": extensions_used,
                }

            # Fast path: network callback has intercepted API response
            if self._response_event.is_set() and self._pending_request_id:
                for body_try in range(4):
                    try:
                        body = await tab.get_network_response_body(self._pending_request_id)
                        self._api_response = json.loads(body)
                        self._record_state(
                            "api_response_captured",
                            attempt=attempt,
                            request_id=self._pending_request_id,
                            body_try=body_try + 1,
                            extensions_used=extensions_used,
                        )
                        print(f"[INFO] API response body retrieved successfully")
                        return {
                            "status": "response_ready",
                            "extensions_used": extensions_used,
                        }
                    except Exception as e:
                        if body_try == 3:
                            self._record_state(
                                "api_response_body_failed",
                                attempt=attempt,
                                error=str(e),
                            )
                            break
                        await asyncio.sleep(poll_interval)

            state = await self._get_runtime_state(tab)
            api_count = int(state.get("apiRequestCount", 0) or 0)
            submit_clicks = int(state.get("submitClicks", 0) or 0)
            event_count = int(state.get("eventCount", 0) or 0)
            api_last_response_at = int(state.get("apiLastResponseAt", 0) or 0)
            api_last_error = (state.get("apiLastError") or "").strip()

            progress_reasons: List[str] = []

            if api_count > observed_api_count:
                observed_api_count = api_count
                request_seen = True
                progress_reasons.append("api_request_count")

            if state.get("apiRequestInFlight"):
                request_seen = True
                progress_reasons.append("api_in_flight")

            if api_last_response_at > observed_last_response_at:
                observed_last_response_at = api_last_response_at
                request_seen = True
                progress_reasons.append("api_response_event")

            if event_count > observed_event_count:
                last_event_type = (state.get("lastEventType") or "").strip()
                if last_event_type.startswith("api_"):
                    request_seen = True
                    progress_reasons.append(last_event_type)
                observed_event_count = event_count

            if api_last_error:
                return {
                    "status": "api_request_error",
                    "error": f"Error de red durante solicitud SUNARP: {api_last_error}",
                    "extensions_used": extensions_used,
                }

            if state.get("captchaError") and not state.get("hasToken") and not request_seen:
                captcha_error_hits += 1
            else:
                captcha_error_hits = 0

            if captcha_error_hits >= (3 if self.slow_mode else 2):
                msg = "Captcha no resuelto."
                if not self.slow_mode:
                    msg += " Sugerencia: usa ?slow=true para conexiones lentas."
                return {
                    "status": "captcha_not_resolved",
                    "error": msg,
                    "extensions_used": extensions_used,
                }

            if progress_reasons:
                now = loop.time()
                last_progress_at = now

                self._record_state(
                    "api_progress",
                    attempt=attempt,
                    reasons=",".join(progress_reasons[:4]),
                    api_count=api_count,
                    in_flight=bool(state.get("apiRequestInFlight")),
                )

                time_left_in_window = active_deadline - now
                if (
                    extensions_used < extensions_allowed
                    and extension_size > 0
                    and time_left_in_window <= max(1.0, extension_size * 0.5)
                ):
                    extend_by = min(extension_size, max(0.0, total_deadline - active_deadline))
                    if extend_by > 0:
                        active_deadline += extend_by
                        extensions_used += 1
                        self._record_state(
                            "api_wait_extended",
                            attempt=attempt,
                            extend_by=round(extend_by, 2),
                            extensions_used=extensions_used,
                            window_remaining=round(active_deadline - now, 2),
                            budget_remaining=round(total_deadline - now, 2),
                        )
            elif not request_seen and (loop.time() - last_progress_at) >= no_progress_fail:
                if submit_clicks > observed_submit_clicks:
                    msg = "SUNARP no envió solicitud al API."
                    if not self.slow_mode:
                        msg += " Sugerencia: usa ?slow=true para conexiones lentas."
                    return {
                        "status": "submission_not_sent",
                        "error": msg,
                        "extensions_used": extensions_used,
                    }

            await asyncio.sleep(poll_interval)
    
    async def _wait_for_cloudflare(self, tab, timeout: int = CLOUDFLARE_TIMEOUT):
        """Wait for Cloudflare/Turnstile to allow interaction (dynamic state-based)."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        poll_interval = 0.45 if self.slow_mode else 0.25
        ready_hits = 0

        # Ensure runtime observers exist in the current document
        await self._inject_runtime_watchers(tab)

        while (loop.time() - start_time) < timeout:
            state = await self._get_runtime_state(tab)

            if state.get("hasPlateInput"):
                ready_hits += 1
                if ready_hits >= 2:
                    self._record_state(
                        "cloudflare_ready",
                        has_turnstile=state.get("hasTurnstile", False),
                        button_found=state.get("buttonFound", False),
                        button_enabled=state.get("buttonEnabled", False),
                    )
                    print("[INFO] Cloudflare bypass complete - input available")
                    return True
            else:
                ready_hits = 0

            await asyncio.sleep(poll_interval)

        self._record_state("cloudflare_timeout", timeout=timeout)
        raise TimeoutError("Cloudflare challenge did not complete in time")
    
    async def _fill_plate_input(self, tab, placa: str) -> bool:
        """Fill the plate number input field using DOM events (dynamic, no fixed sleeps)."""
        plate_json = json.dumps(placa)
        fill_script = f"""
        (function() {{
            const targetPlate = {plate_json};
            const selectors = [
                "input#placa",
                "input[name='placa']",
                "input[formcontrolname='placa']",
                "input[placeholder*='ABC']",
                "input[type='text']"
            ];

            let input = null;
            let selectorUsed = null;
            for (const selector of selectors) {{
                const found = document.querySelector(selector);
                if (found) {{
                    input = found;
                    selectorUsed = selector;
                    break;
                }}
            }}

            if (!input) {{
                return JSON.stringify({{ ok: false, reason: 'input-not-found' }});
            }}

            input.focus();
            input.value = '';
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));

            input.value = targetPlate;
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'Enter' }}));

            return JSON.stringify({{
                ok: true,
                selector: selectorUsed,
                value: input.value || ''
            }});
        }})();
        """

        filled = await self._run_json_script(tab, fill_script, default={"ok": False})
        if filled.get("ok"):
            selector = filled.get("selector", "unknown")
            self._record_state("plate_filled", selector=selector, value=filled.get("value", ""))
            print(f"[INFO] Plate entered using selector: {selector}")
            return True

        # Fallback to pydoll typing if direct DOM injection failed
        input_elem = await tab.query("input[type='text']", timeout=0, raise_exc=False)
        if input_elem:
            try:
                await input_elem.click()
                await input_elem.type_text(placa)
                self._record_state("plate_filled_fallback", selector="input[type='text']")
                print("[INFO] Plate entered using fallback text input")
                return True
            except Exception:
                pass

        self._record_state("plate_fill_failed")
        return False
    
    async def _wait_for_plate_value(self, tab, placa: str, timeout: float = 20) -> bool:
        """Wait until the plate input reflects the target value."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        poll_interval = 0.45 if self.slow_mode else 0.25
        target = placa.strip().upper()

        while (loop.time() - start_time) < timeout:
            state = await self._get_runtime_state(tab)
            current = (state.get("plateValue") or "").strip().upper()
            if current == target:
                self._record_state("plate_confirmed", value=current)
                return True
            await asyncio.sleep(poll_interval)

        self._record_state("plate_confirm_timeout", expected=target)
        return False

    async def _wait_for_submission_gate(
        self,
        tab,
        attempt: int,
        timeout_seconds: float,
        require_token: bool = False,
    ) -> Dict[str, Any]:
        """Wait until submit gate is ready, with strict token mode on retries when needed."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        timeout = _positive_float(timeout_seconds, CAPTCHA_WAIT_TIMEOUT, minimum=0.8)
        poll_interval = 0.45 if self.slow_mode else 0.22

        saw_turnstile = False
        strict_token = bool(require_token)
        stable_no_turnstile_hits = 0
        captcha_error_hits = 0
        last_state: Dict[str, Any] = {}

        self._record_state(
            "submit_gate_wait_start",
            attempt=attempt,
            timeout_seconds=round(timeout, 2),
            require_token=bool(require_token),
        )

        while (loop.time() - start_time) < timeout:
            state = await self._get_runtime_state(tab)
            last_state = state

            if state.get("hasTurnstile"):
                saw_turnstile = True
                strict_token = True
                stable_no_turnstile_hits = 0

            if state.get("hasToken"):
                elapsed = loop.time() - start_time
                print(f"[INFO] Captcha completed in {elapsed:.1f}s (token: {state.get('tokenLength', 0)} chars)")
                return {
                    "ready": True,
                    "gate_reason": "token_ready",
                    **state,
                }

            if state.get("captchaError") and not state.get("hasToken"):
                captcha_error_hits += 1
                strict_token = True
                stable_no_turnstile_hits = 0

                if captcha_error_hits >= (3 if self.slow_mode else 2):
                    msg = "Captcha no resuelto."
                    if not self.slow_mode:
                        msg += " Sugerencia: usa ?slow=true para conexiones lentas."
                    return {
                        "ready": False,
                        "error": msg,
                        **state,
                    }

                await asyncio.sleep(poll_interval)
                continue

            captcha_error_hits = 0

            # Allow no-turnstile path only when gate is stable and token is not required.
            if (
                not strict_token
                and not state.get("hasTurnstile")
                and state.get("buttonEnabled")
                and state.get("hasPlateInput")
                and not state.get("captchaError")
            ):
                stable_no_turnstile_hits += 1
                elapsed = loop.time() - start_time
                min_hits = 8 if self.slow_mode else 6
                min_elapsed = 2.5 if self.slow_mode else 1.2

                if stable_no_turnstile_hits >= min_hits and elapsed >= min_elapsed:
                    print("[INFO] Turnstile not visible, proceeding with guarded submit")
                    return {
                        "ready": True,
                        "gate_reason": "no_turnstile_stable",
                        **state,
                    }
            else:
                stable_no_turnstile_hits = 0

            await asyncio.sleep(poll_interval)

        if strict_token or saw_turnstile:
            msg = "Captcha no resuelto antes de enviar consulta"
            if not self.slow_mode:
                msg += ". Sugerencia: usa ?slow=true"
        else:
            msg = "Formulario no quedó listo para enviar consulta"

        return {
            "ready": False,
            "error": msg,
            **last_state,
        }

    async def _click_search_button(self, tab) -> bool:
        """Perform a single guarded click on search button and record submit intent."""
        click_script = """
        (function() {
            const runtime = window.__sunarpRuntime || (window.__sunarpRuntime = {});
            runtime.submitClicks = (runtime.submitClicks || 0) + 1;
            runtime.lastSubmitAt = Date.now();
            if (typeof runtime.pushEvent === 'function') {
                runtime.pushEvent('submit_click_attempt', { count: runtime.submitClicks });
            }

            let btn = document.querySelector('button.btn-sunarp-green, button.ant-btn-primary');

            if (!btn) {
                const buttons = document.querySelectorAll('button');
                for (const candidate of buttons) {
                    const text = (candidate.innerText || '').toLowerCase();
                    if (text.includes('busqueda') || text.includes('buscar') || text.includes('consulta')) {
                        btn = candidate;
                        break;
                    }
                }
            }

            if (!btn) {
                return JSON.stringify({ clicked: false, reason: 'button-not-found' });
            }

            btn.disabled = false;
            btn.removeAttribute('disabled');
            btn.click();

            if (typeof runtime.pushEvent === 'function') {
                runtime.pushEvent('submit_clicked', {
                    text: (btn.innerText || '').trim().slice(0, 60)
                });
            }

            return JSON.stringify({
                clicked: true,
                text: (btn.innerText || '').trim().slice(0, 60)
            });
        })();
        """

        click_result = await self._run_json_script(tab, click_script, default={"clicked": False})
        clicked = bool(click_result.get("clicked"))
        if clicked:
            self._record_state("submit_clicked", button_text=click_result.get("text", ""))
            print("[INFO] Search button clicked")
            return True

        self._record_state("submit_click_failed", reason=click_result.get("reason", "unknown"))
        return False
    
    async def _process_api_response(self, response_data: Dict[str, Any], placa: str) -> ConsultaResult:
        """Process the intercepted API response and save the image."""
        result = ConsultaResult(
            placa=placa,
            raw_response=response_data,
        )
        
        # Extract metadata
        result.cod = response_data.get("cod", 0)
        result.mensaje = response_data.get("mensaje", "")
        result.mensaje_txt = response_data.get("mensajeTxt", "")
        result.icon = response_data.get("icon", "")
        result.alerta_robo = response_data.get("msgAlertaRobo", "")
        
        # Extract sedes
        sedes_data = response_data.get("sedes", [])
        result.sedes = [SedeInfo.from_dict(s) for s in sedes_data]
        
        # Check if successful
        if result.cod != 1:
            result.success = False
            result.error = result.mensaje or "Unknown error"
            return result
        
        # Extract and save image
        model = response_data.get("model", {})
        image_base64 = model.get("imagen")
        
        if not image_base64:
            result.success = False
            result.error = "No image in response"
            return result
        
        # Save image
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sunarp_{placa}_{timestamp}.png"
        filepath = self.downloads_dir / filename
        
        try:
            image_bytes = base64.b64decode(image_base64)
            with open(filepath, "wb") as f:
                f.write(image_bytes)
            
            result.success = True
            result.image_path = str(filepath)
            print(f"[INFO] Image saved to: {filepath}")
            
            # Perform OCR extraction
            try:
                from ocr import extract_vehicle_data
                
                print(f"[INFO] Running OCR on image...")
                ocr_result = extract_vehicle_data(str(filepath))
                
                if ocr_result["success"] and ocr_result["data"]:
                    result.ocr_data = VehicleOCRData.from_dict(ocr_result["data"])
                    print(f"[INFO] OCR extraction successful")
                else:
                    result.ocr_error = ocr_result.get("error", "OCR extraction failed")
                    print(f"[WARN] OCR extraction failed: {result.ocr_error}")
                    
            except ImportError as e:
                result.ocr_error = f"OCR module not available: {e}"
                print(f"[WARN] OCR module import error: {e}")
            except Exception as e:
                result.ocr_error = f"OCR error: {str(e)}"
                print(f"[WARN] OCR error: {e}")
            
        except Exception as e:
            result.success = False
            result.error = f"Failed to save image: {e}"
        
        return result


async def main():
    """Test the scraper directly."""
    import sys
    
    # Parse command-line arguments
    headless = "--headless" in sys.argv
    slow_mode = "--slow" in sys.argv
    
    if headless:
        print("[INFO] Running in HEADLESS mode")
    if slow_mode:
        print("[INFO] Running in SLOW mode (longer timeouts)")
    
    scraper = SunarpScraper(headless=headless, slow_mode=slow_mode)
    
    # Get plate from argument or prompt
    placa = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            placa = arg
            break
    
    if not placa:
        placa = input("Enter plate number to query: ").strip()
    if not placa:
        placa = "ABC123"
    
    try:
        result = await scraper.consultar_placa(placa)
        
        if result.success:
            print(f"\n[SUCCESS] Image saved to: {result.image_path}")
            print(f"  Mensaje: {result.mensaje}")
            print(f"  Alerta Robo: {result.alerta_robo or 'None'}")
            if result.sedes:
                print(f"  Sedes ({len(result.sedes)}):")
                for sede in result.sedes:
                    print(f"    - {sede.nombre} (Partida: {sede.num_partida})")
        else:
            print(f"\n[ERROR] {result.error}")
            
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("Usage: python scraper.py [PLATE] [--headless] [--slow]")
    print("  PLATE: Vehicle plate number (e.g., ABC123)")
    print("  --headless: Run browser without visible window (may fail with Turnstile)")
    print("  --slow: Use longer timeouts for slow internet connections")
    print()
    asyncio.run(main())
