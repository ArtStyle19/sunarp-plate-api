"""
SUNARP Vehicle Consultation API

FastAPI server that exposes an endpoint to query vehicle information
from SUNARP and return the result as an image.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from scraper import SunarpScraper, ConsultaResult, DOWNLOADS_DIR

# Load environment variables from .env file
load_dotenv()

# Configuration via environment variables
HEADLESS_MODE = os.getenv("SUNARP_HEADLESS", "false").lower() == "true"
SLOW_MODE = os.getenv("SUNARP_SLOW_MODE", "false").lower() == "true"
API_HOST = os.getenv("SUNARP_HOST", "0.0.0.0")
API_PORT = int(os.getenv("SUNARP_PORT", "8000"))

# Global scraper instance (single request at a time)
scraper: Optional[SunarpScraper] = None
scraper_lock = asyncio.Lock()


def get_scraper(headless: bool = None, slow_mode: bool = None) -> SunarpScraper:
    """Get or create the scraper instance."""
    global scraper
    
    # Use environment defaults if not specified
    use_headless = headless if headless is not None else HEADLESS_MODE
    use_slow_mode = slow_mode if slow_mode is not None else SLOW_MODE
    
    # Recreate scraper if settings changed
    if scraper is None or scraper.headless != use_headless or scraper.slow_mode != use_slow_mode:
        scraper = SunarpScraper(headless=use_headless, slow_mode=use_slow_mode)
        mode_info = []
        if use_headless:
            mode_info.append("headless")
        if use_slow_mode:
            mode_info.append("slow-mode")
        mode_str = " (" + ", ".join(mode_info) + ")" if mode_info else ""
        print(f"[INFO] Created scraper instance{mode_str}")
    
    return scraper


def cleanup_old_images(max_age_hours: int = 24):
    """Remove images older than max_age_hours."""
    import time
    
    if not DOWNLOADS_DIR.exists():
        return
    
    current_time = time.time()
    max_age_seconds = max_age_hours * 3600
    
    for file in DOWNLOADS_DIR.glob("*.png"):
        try:
            file_age = current_time - file.stat().st_mtime
            if file_age > max_age_seconds:
                file.unlink()
                print(f"[CLEANUP] Removed old file: {file.name}")
        except Exception as e:
            print(f"[CLEANUP] Error removing {file}: {e}")
    
    # Also clean up jpg files
    for file in DOWNLOADS_DIR.glob("*.jpg"):
        try:
            file_age = current_time - file.stat().st_mtime
            if file_age > max_age_seconds:
                file.unlink()
                print(f"[CLEANUP] Removed old file: {file.name}")
        except Exception as e:
            print(f"[CLEANUP] Error removing {file}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    print(f"[INFO] Downloads directory: {DOWNLOADS_DIR}")
    print(f"[INFO] API ready at http://localhost:8000")
    print(f"[INFO] Docs available at http://localhost:8000/docs")
    
    yield  # Server is running
    
    # Shutdown (cleanup if needed)
    print("[INFO] Shutting down...")


# Initialize FastAPI app with lifespan
app = FastAPI(
    title="SUNARP Vehicle Consultation API",
    description="API to query vehicle information from SUNARP (Peru)",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware for browser access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": "SUNARP Vehicle Consultation API",
        "version": "1.0.0",
        "endpoints": {
            "/consulta/{placa}": "Query vehicle by plate number and get image",
            "/consulta/{placa}/json": "Query vehicle and get JSON with image path",
            "/consulta/{placa}/full": "Query vehicle and get complete metadata + OCR data",
            "/ocr/{filename}": "Run OCR on an existing image",
            "/ocr/{filename}/debug": "Debug OCR extraction with all methods",
            "/images/{filename}": "Retrieve a previously captured image",
            "/health": "Health check endpoint",
            "/docs": "Interactive API documentation",
        },
        "example": "GET /consulta/ABC123",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "downloads_dir": str(DOWNLOADS_DIR)}


@app.get("/consulta/{placa}")
async def consultar_vehiculo(
    placa: str,
    background_tasks: BackgroundTasks,
    download: bool = True,
    slow: Optional[bool] = Query(None, description="Use longer timeouts for slow internet connections"),
):
    """
    Query vehicle information from SUNARP by plate number.
    
    Args:
        placa: Vehicle plate number (e.g., "ABC123")
        download: If True, return the image file. If False, return JSON with file path.
        slow: If True, use longer timeouts for slow internet connections.
    
    Returns:
        The vehicle information image or JSON with the file path.
    """
    # Validate plate format (basic validation)
    placa = placa.strip().upper()
    if not placa or len(placa) < 3 or len(placa) > 10:
        raise HTTPException(
            status_code=400,
            detail="Invalid plate number format. Expected 3-10 characters."
        )
    
    # Use lock to ensure only one request at a time
    async with scraper_lock:
        try:
            effective_slow = SLOW_MODE if slow is None else slow
            print(f"\n[API] Starting consultation for plate: {placa}" + (" [slow mode]" if effective_slow else ""))
            
            # Get scraper and perform consultation
            scraper_instance = get_scraper(slow_mode=effective_slow)
            result: ConsultaResult = await scraper_instance.consultar_placa(placa)
            
            if not result.success:
                error_text = (result.error or "").lower()
                if "captcha no resuelto" in error_text:
                    raise HTTPException(
                        status_code=409,
                        detail=result.error
                    )
                if "timeout waiting for api response" in error_text:
                    raise HTTPException(
                        status_code=504,
                        detail="Timeout waiting for SUNARP API response. Intenta con ?slow=true"
                    )
                raise HTTPException(
                    status_code=404 if result.cod == 0 else 500,
                    detail=result.error or result.mensaje or "Consultation failed"
                )
            
            if not result.image_path or not os.path.exists(result.image_path):
                raise HTTPException(
                    status_code=500,
                    detail="Failed to capture result image"
                )
            
            print(f"[API] Consultation complete. Image: {result.image_path}")
            
            # Schedule cleanup of old images
            background_tasks.add_task(cleanup_old_images)
            
            # Determine media type based on file extension
            media_type = "image/png"
            if result.image_path.endswith(".jpg") or result.image_path.endswith(".jpeg"):
                media_type = "image/jpeg"
            
            if download:
                # Return the image file directly
                return FileResponse(
                    path=result.image_path,
                    media_type=media_type,
                    filename=os.path.basename(result.image_path),
                )
            else:
                # Return JSON with file path
                return JSONResponse({
                    "success": True,
                    "placa": placa,
                    "image_path": result.image_path,
                    "filename": os.path.basename(result.image_path),
                })
                
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="Request timed out. The SUNARP page may be slow or Cloudflare challenge failed."
            )
        except Exception as e:
            print(f"[API] Error: {type(e).__name__}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error during consultation: {str(e)}"
            )


@app.get("/consulta/{placa}/json")
async def consultar_vehiculo_json(
    placa: str,
    background_tasks: BackgroundTasks,
    slow: Optional[bool] = Query(None, description="Use longer timeouts for slow internet connections"),
):
    """
    Query vehicle and return JSON response with file path instead of the image.
    """
    return await consultar_vehiculo(placa, background_tasks, download=False, slow=slow)


@app.get("/consulta/{placa}/full")
async def consultar_vehiculo_full(
    placa: str,
    background_tasks: BackgroundTasks,
    slow: Optional[bool] = Query(None, description="Use longer timeouts for slow internet connections"),
):
    """
    Query vehicle and return complete metadata including sedes, alerts, and image path.
    
    This endpoint returns all the data extracted from the SUNARP API response:
    - Image file path
    - Response codes and messages
    - Alerta de robo (theft alert)
    - List of SUNARP offices (sedes) where the vehicle is registered
    
    Args:
        placa: Vehicle plate number (e.g., "ABC123")
        slow: If True, use longer timeouts for slow internet connections.
    
    Returns:
        JSON with complete vehicle consultation data.
    """
    # Validate plate format
    placa = placa.strip().upper()
    if not placa or len(placa) < 3 or len(placa) > 10:
        raise HTTPException(
            status_code=400,
            detail="Invalid plate number format. Expected 3-10 characters."
        )
    
    # Use lock to ensure only one request at a time
    async with scraper_lock:
        try:
            effective_slow = SLOW_MODE if slow is None else slow
            print(f"\n[API] Starting full consultation for plate: {placa}" + (" [slow mode]" if effective_slow else ""))
            
            # Get scraper and perform consultation
            scraper_instance = get_scraper(slow_mode=effective_slow)
            result: ConsultaResult = await scraper_instance.consultar_placa(placa)
            
            # Schedule cleanup of old images
            background_tasks.add_task(cleanup_old_images)
            
            # Return complete result as JSON
            return JSONResponse(result.to_dict())
            
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="Request timed out. The SUNARP page may be slow or Cloudflare challenge failed."
            )
        except Exception as e:
            print(f"[API] Error: {type(e).__name__}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error during consultation: {str(e)}"
            )


@app.get("/images/{filename}")
async def get_image(filename: str):
    """
    Retrieve a previously captured image by filename.
    """
    filepath = DOWNLOADS_DIR / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    
    # Security check: ensure file is within downloads directory
    try:
        filepath.resolve().relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Determine media type
    media_type = "image/png"
    if filename.endswith(".jpg") or filename.endswith(".jpeg"):
        media_type = "image/jpeg"
    
    return FileResponse(
        path=str(filepath),
        media_type=media_type,
        filename=filename,
    )


@app.delete("/images/{filename}")
async def delete_image(filename: str):
    """
    Delete a captured image.
    """
    filepath = DOWNLOADS_DIR / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    
    try:
        filepath.resolve().relative_to(DOWNLOADS_DIR.resolve())
        filepath.unlink()
        return {"success": True, "message": f"Deleted {filename}"}
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ocr/{filename}")
async def ocr_image(filename: str):
    """
    Run OCR extraction on an existing image.
    
    Args:
        filename: Name of the image file in the downloads directory
        
    Returns:
        JSON with extracted vehicle data
    """
    filepath = DOWNLOADS_DIR / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    
    # Security check
    try:
        filepath.resolve().relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        from ocr import extract_vehicle_data
        
        result = extract_vehicle_data(str(filepath))
        return JSONResponse(result)
        
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"OCR module not available. Install dependencies: {e}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"OCR error: {str(e)}"
        )


@app.get("/ocr/{filename}/debug")
async def ocr_image_debug(filename: str):
    """
    Debug OCR extraction - shows results from all preprocessing methods.
    
    Useful for troubleshooting OCR issues and comparing different approaches.
    
    Args:
        filename: Name of the image file in the downloads directory
        
    Returns:
        JSON with raw OCR output from multiple methods
    """
    filepath = DOWNLOADS_DIR / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    
    # Security check
    try:
        filepath.resolve().relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        from ocr import extract_vehicle_data_debug
        
        result = extract_vehicle_data_debug(str(filepath))
        return JSONResponse(result)
        
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"OCR module not available. Install dependencies: {e}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"OCR error: {str(e)}"
        )


def main():
    """Run the API server."""
    print("=" * 60)
    print("SUNARP Vehicle Consultation API")
    print("=" * 60)
    print(f"\nConfiguration (from .env):")
    print(f"  SUNARP_HEADLESS: {HEADLESS_MODE}")
    print(f"  SUNARP_SLOW_MODE: {SLOW_MODE}")
    print(f"  SUNARP_HOST: {API_HOST}")
    print(f"  SUNARP_PORT: {API_PORT}")
    print(f"\nStarting server...")
    print(f"API will be available at: http://{API_HOST}:{API_PORT}")
    print(f"Documentation at: http://{API_HOST}:{API_PORT}/docs")
    print("\nExample usage:")
    print(f"  curl http://localhost:{API_PORT}/consulta/ABC123 -o result.png")
    print(f"  curl http://localhost:{API_PORT}/consulta/ABC123/json")
    print(f"  curl http://localhost:{API_PORT}/consulta/ABC123/full  # Complete metadata")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 60 + "\n")
    
    uvicorn.run(
        "main:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
