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
CHROMIUM_PATH = "/usr/bin/chromium-browser"

# Timeouts (in seconds) - generous for slow connections
PAGE_LOAD_TIMEOUT = 60       # Increased for slow internet
CLOUDFLARE_TIMEOUT = 180     # Turnstile can be slow on bad connections
RESULT_TIMEOUT = 90          # API response timeout
CAPTCHA_WAIT_TIMEOUT = 60    # Time to wait for captcha to auto-solve


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
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
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
        }


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
    
    def _get_browser_options(self) -> ChromiumOptions:
        """Configure Chrome options to appear as genuine Google Chrome."""
        options = ChromiumOptions()
        
        # Set browser path
        if os.path.exists(CHROMIUM_PATH):
            options.binary_location = CHROMIUM_PATH
        
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
        self._api_response = None
        self._response_event.clear()
        self._pending_request_id = None
        
        options = self._get_browser_options()
        
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            self._tab = tab
            
            # Adaptive delays based on slow_mode
            nav_delay = 4 if self.slow_mode else 2      # After navigation
            result_timeout = RESULT_TIMEOUT * 1.5 if self.slow_mode else RESULT_TIMEOUT
            
            # Enable page events to track navigation
            print(f"[INFO] Enabling page events...")
            await tab.enable_page_events()
            
            # Register callback to re-inject scripts on each page load
            await tab.on(PageEvent.FRAME_NAVIGATED, self._on_frame_navigated)
            await tab.on(PageEvent.LOAD_EVENT_FIRED, self._on_frame_navigated)
            
            # Inject anti-detection BEFORE any navigation
            print(f"[INFO] Injecting anti-detection scripts...")
            await self._inject_anti_detection(tab)
            
            # Enable network events to intercept API responses
            print(f"[INFO] Enabling network interception...")
            await tab.enable_network_events()
            
            # Register callback for network responses
            await tab.on(NetworkEvent.RESPONSE_RECEIVED, self._on_response_received)
            
            # Navigate to the base domain first (helps with session/cookies)
            print(f"[INFO] Navigating to base domain...")
            await tab.go_to("https://consultavehicular.sunarp.gob.pe/")
            await asyncio.sleep(nav_delay)
            
            print(f"[INFO] Waiting for page elements to load...")
            cf_timeout = CLOUDFLARE_TIMEOUT * 1.5 if self.slow_mode else CLOUDFLARE_TIMEOUT
            await self._wait_for_cloudflare(tab, timeout=int(cf_timeout))
            
            print(f"[INFO] Filling plate number: {placa}")
            await self._fill_plate_input(tab, placa)
            
            # Small delay after typing to let Angular process the input
            await asyncio.sleep(0.5)
            
            # Reset the response state before clicking (ignore any previous API calls)
            self._api_response = None
            self._response_event.clear()
            self._pending_request_id = None
            
            print(f"[INFO] Clicking search button...")
            await self._click_search_button(tab)
            
            print(f"[INFO] Waiting for API response...")
            # Wait for the API response to be intercepted
            try:
                await asyncio.wait_for(
                    self._wait_for_api_response(tab),
                    timeout=result_timeout
                )
            except asyncio.TimeoutError:
                return ConsultaResult(
                    success=False,
                    placa=placa,
                    error="Timeout waiting for API response"
                )
            
            # Process the intercepted response
            if self._api_response:
                print(f"[INFO] Processing API response...")
                return await self._process_api_response(self._api_response, placa)
            else:
                return ConsultaResult(
                    success=False,
                    placa=placa,
                    error="No API response received"
                )
    
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
    
    async def _wait_for_api_response(self, tab):
        """Wait for the API response and retrieve its body."""
        # Wait for response received event
        await self._response_event.wait()
        
        # Small delay to ensure response body is available
        await asyncio.sleep(1)
        
        # Get the response body
        if self._pending_request_id:
            try:
                body = await tab.get_network_response_body(self._pending_request_id)
                self._api_response = json.loads(body)
                print(f"[INFO] API response body retrieved successfully")
            except Exception as e:
                print(f"[ERROR] Failed to get response body: {e}")
                self._api_response = None
    
    async def _wait_for_cloudflare(self, tab, timeout: int = CLOUDFLARE_TIMEOUT):
        """Wait for Cloudflare challenge to complete."""
        start_time = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            try:
                # Check for the specific SUNARP input using query with find_all=True
                plate_input = await tab.query(
                    "input#placa, input[name='placa'], input[formcontrolname='placa']",
                    timeout=0,
                    raise_exc=False
                )
                if plate_input:
                    print("[INFO] Cloudflare bypass complete - found plate input!")
                    return True
                
                # Alternative: look for any text input
                text_input = await tab.query(
                    "input[type='text']",
                    timeout=0,
                    raise_exc=False
                )
                if text_input:
                    print("[INFO] Cloudflare bypass complete - found text input!")
                    return True
                
                # Check for search button by text
                button = await tab.find(
                    tag_name="button",
                    text="Búsqueda",
                    timeout=0,
                    raise_exc=False
                )
                if button:
                    print("[INFO] Cloudflare bypass complete - found search button!")
                    return True
                        
            except Exception as e:
                print(f"[DEBUG] Waiting for Cloudflare: {e}")
            
            await asyncio.sleep(2)
        
        raise TimeoutError("Cloudflare challenge did not complete in time")
    
    async def _fill_plate_input(self, tab, placa: str):
        """Fill the plate number input field."""
        selectors = [
            "input#placa",
            "input[name='placa']",
            "input[formcontrolname='placa']",
            "input[placeholder*='ABC']",
        ]
        
        for selector in selectors:
            try:
                input_elem = await tab.query(selector, timeout=0, raise_exc=False)
                if input_elem:
                    await input_elem.click()
                    await asyncio.sleep(0.3)
                    await input_elem.type_text(placa)
                    print(f"[INFO] Plate entered using selector: {selector}")
                    return
            except Exception:
                continue
        
        # Fallback: find any text input
        input_elem = await tab.query("input[type='text']", timeout=0, raise_exc=False)
        if input_elem:
            await input_elem.click()
            await asyncio.sleep(0.3)
            await input_elem.type_text(placa)
            print("[INFO] Plate entered using fallback text input")
            return
        
        raise Exception("Could not find plate input field")
    
    async def _click_search_button(self, tab):
        """Click the search/busqueda button after waiting for Turnstile captcha to complete."""
        
        # Helper to extract value from execute_script result
        def get_script_value(result):
            """Extract the actual value from pydoll's execute_script response."""
            if isinstance(result, dict):
                if 'result' in result:
                    inner = result['result']
                    if isinstance(inner, dict) and 'result' in inner:
                        inner = inner['result']
                    if isinstance(inner, dict) and 'value' in inner:
                        return inner['value']
                    return inner
            return result
        
        # Fast captcha detection with shorter polling
        max_captcha_wait = 30 if self.slow_mode else 15  # Reduced from 30s
        poll_interval = 0.2  # Faster polling (was 0.5s)
        
        print("[INFO] Waiting for Turnstile captcha...")
        
        captcha_completed = False
        start_time = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start_time) < max_captcha_wait:
            try:
                # Combined check: captcha token OR button already enabled
                check_result = await tab.execute_script("""
                    // Check for Turnstile response token
                    const turnstileInput = document.querySelector('input[name="cf-turnstile-response"]');
                    const hasToken = turnstileInput && turnstileInput.value && turnstileInput.value.length > 10;
                    
                    // Check if button is already enabled
                    const btn = document.querySelector('button.btn-sunarp-green');
                    const btnEnabled = btn && !btn.disabled;
                    
                    // Check if turnstile iframe exists (still loading)
                    const turnstileFrame = document.querySelector('iframe[src*="turnstile"]');
                    const hasTurnstile = !!turnstileFrame;
                    
                    return JSON.stringify({
                        hasToken: hasToken,
                        tokenLength: hasToken ? turnstileInput.value.length : 0,
                        btnEnabled: btnEnabled,
                        hasTurnstile: hasTurnstile
                    });
                """)
                
                status = get_script_value(check_result)
                if isinstance(status, str):
                    import json
                    status = json.loads(status)
                
                # If we have token OR button is enabled, we're good
                if status.get('hasToken') or status.get('btnEnabled'):
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if status.get('hasToken'):
                        print(f"[INFO] Captcha completed in {elapsed:.1f}s (token: {status.get('tokenLength')} chars)")
                    else:
                        print(f"[INFO] Button enabled in {elapsed:.1f}s")
                    captcha_completed = True
                    break
                
                # If no turnstile at all, proceed
                if not status.get('hasTurnstile'):
                    print("[INFO] No Turnstile detected, proceeding...")
                    captcha_completed = True
                    break
                    
            except Exception as e:
                pass  # Silent retry
            
            await asyncio.sleep(poll_interval)
        
        if not captcha_completed:
            print("[WARN] Captcha wait timed out, attempting click anyway...")
        
        # Minimal delay before clicking
        await asyncio.sleep(0.3)
        
        # Click the button
        print("[INFO] Clicking search button...")
        
        result = await tab.execute_script("""
            const btn = document.querySelector('button.btn-sunarp-green');
            if (btn) {
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.click();
                return 'clicked';
            }
            return 'not-found';
        """)
        clicked = get_script_value(result)
        
        if clicked == 'clicked':
            print("[INFO] Search button clicked")
            return
        
        # Fallback: try ant-btn-primary
        result = await tab.execute_script("""
            const btn = document.querySelector('button.ant-btn-primary');
            if (btn) {
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.click();
                return 'clicked';
            }
            return 'not-found';
        """)
        clicked = get_script_value(result)
        
        if clicked == 'clicked':
            print("[INFO] Search button clicked (fallback)")
            return
        
        # Last resort: find by text
        result = await tab.execute_script("""
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = (btn.innerText || '').toLowerCase();
                if (text.includes('busqueda') || text.includes('buscar')) {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.click();
                    return 'clicked';
                }
            }
            return 'not-found';
        """)
        clicked = get_script_value(result)
        
        if clicked == 'clicked':
            print("[INFO] Search button clicked (by text)")
            return
        
        raise Exception("Could not find or click search button")
    
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
