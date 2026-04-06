"""
SUNARP Vehicle Image OCR Module

Extracts text from SUNARP vehicle consultation images and parses
into structured JSON data. Handles the diagonal watermark overlay.
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
import pytesseract
from PIL import Image


# Field mappings from Spanish labels to English keys
# Order matters for some - more specific patterns first
FIELD_MAPPINGS = {
    # Placa variations
    "N° PLACA": "placa",
    "Nº PLACA": "placa",
    "NO PLACA": "placa",
    "N PLACA": "placa",
    "N* PLACA": "placa",
    # Serie variations
    "N° SERIE": "serie",
    "Nº SERIE": "serie",
    "NO SERIE": "serie",
    "N SERIE": "serie",
    "N* SERIE": "serie",
    # VIN variations (OCR often reads V as Y)
    "N° VIN": "vin",
    "Nº VIN": "vin",
    "NO VIN": "vin",
    "N VIN": "vin",
    "N* VIN": "vin",
    "N° YIN": "vin",
    "Nº YIN": "vin",
    "NO YIN": "vin",
    "N YIN": "vin",
    "N* YIN": "vin",
    # Motor variations
    "N° MOTOR": "motor",
    "Nº MOTOR": "motor",
    "N°MOTOR": "motor",
    "NºMOTOR": "motor",
    "NO MOTOR": "motor",
    "N MOTOR": "motor",
    "N* MOTOR": "motor",
    "NMOTOR": "motor",
    # Year of model - must come BEFORE "MODELO" to match correctly
    "AÑO DE MODELO": "anio_modelo",
    "ANO DE MODELO": "anio_modelo",
    "ANO MODELO": "anio_modelo",
    "AÑO MODELO": "anio_modelo",
    # Other fields
    "COLOR": "color",
    "MARCA": "marca",
    "MODELO": "modelo",
    "PLACA VIGENTE": "placa_vigente",
    "PLACA ANTERIOR": "placa_anterior",
    "ESTADO": "estado",
    "ANOTACIONES": "anotaciones",
    "SEDE": "sede",
    "PROPIETARIO": "propietario",
    "PROPIETARIO(S)": "propietario",
    "PROPIETARIOS": "propietario",
}


def preprocess_image(image_path: str) -> np.ndarray:
    """
    Preprocess the SUNARP image to improve OCR accuracy.
    
    The image has:
    - Green header with SUNARP logo (skip this area)
    - White background with black text
    - Diagonal gray watermark text overlay
    
    Strategy:
    1. Convert to grayscale
    2. Apply thresholding to remove light watermark
    3. Enhance contrast for text
    """
    # Read image
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image: {image_path}")
    
    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Get image dimensions
    height, width = gray.shape
    
    # Crop to remove the header (approximately top 15-18% is the green header)
    header_ratio = 0.16
    crop_top = int(height * header_ratio)
    gray_cropped = gray[crop_top:, :]
    
    # Apply bilateral filter to reduce noise while keeping edges
    denoised = cv2.bilateralFilter(gray_cropped, 9, 75, 75)
    
    # The watermark is lighter gray text, so we use thresholding
    # to keep only the darker text (main content)
    # Using adaptive threshold works better for varying lighting
    
    # First, increase contrast
    # CLAHE (Contrast Limited Adaptive Histogram Equalization)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    contrast = clahe.apply(denoised)
    
    # Binary threshold - the main text is much darker than watermark
    # The watermark appears as light gray (~200-230 in grayscale)
    # The main text is black (~0-50)
    _, binary = cv2.threshold(contrast, 180, 255, cv2.THRESH_BINARY)
    
    # Alternative: Adaptive thresholding for better local contrast
    adaptive = cv2.adaptiveThreshold(
        contrast,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11,  # Block size
        2    # C constant
    )
    
    # Combine both methods - use the one that produces cleaner result
    # For watermark removal, standard threshold usually works better
    result = binary
    
    # Optional: Morphological operations to clean up
    kernel = np.ones((1, 1), np.uint8)
    result = cv2.morphologyEx(result, cv2.MORPH_CLOSE, kernel)
    
    return result


def preprocess_image_aggressive(image_path: str) -> np.ndarray:
    """
    More aggressive preprocessing for difficult images.
    Uses multiple techniques to remove watermark.
    """
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image: {image_path}")
    
    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    height, width = gray.shape
    
    # Crop header
    header_ratio = 0.16
    crop_top = int(height * header_ratio)
    gray_cropped = gray[crop_top:, :]
    
    # Method: High threshold to eliminate watermark completely
    # Watermark is typically 180-220 gray, text is 0-80
    _, high_thresh = cv2.threshold(gray_cropped, 150, 255, cv2.THRESH_BINARY)
    
    # Invert if needed (tesseract prefers black text on white)
    # Check if image is mostly white or black
    white_ratio = np.sum(high_thresh == 255) / high_thresh.size
    if white_ratio < 0.5:
        high_thresh = cv2.bitwise_not(high_thresh)
    
    return high_thresh


def extract_text_raw(image_path: str, preprocess: bool = True) -> str:
    """
    Extract raw text from image using OCR.
    
    Args:
        image_path: Path to the image file
        preprocess: Whether to apply preprocessing
        
    Returns:
        Raw extracted text
    """
    if preprocess:
        try:
            # Try standard preprocessing first
            processed = preprocess_image(image_path)
        except Exception:
            # Fall back to aggressive preprocessing
            processed = preprocess_image_aggressive(image_path)
        
        # Convert numpy array to PIL Image for pytesseract
        pil_image = Image.fromarray(processed)
    else:
        pil_image = Image.open(image_path)
    
    # Configure tesseract for Spanish
    # PSM 6 = Assume a single uniform block of text
    # PSM 4 = Assume a single column of text of variable sizes
    config = r'--oem 3 --psm 6 -l spa'
    
    try:
        text = pytesseract.image_to_string(pil_image, config=config)
    except Exception as e:
        # Try with default English if Spanish not available
        config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(pil_image, config=config)
    
    return text


def extract_text_with_boxes(image_path: str) -> Dict[str, Any]:
    """
    Extract text with bounding box information for better parsing.
    """
    processed = preprocess_image(image_path)
    pil_image = Image.fromarray(processed)
    
    config = r'--oem 3 --psm 6 -l spa'
    
    try:
        data = pytesseract.image_to_data(pil_image, config=config, output_type=pytesseract.Output.DICT)
    except Exception:
        config = r'--oem 3 --psm 6'
        data = pytesseract.image_to_data(pil_image, config=config, output_type=pytesseract.Output.DICT)
    
    return data


def clean_text(text: str) -> str:
    """Clean OCR artifacts from extracted text."""
    # Remove common OCR errors
    text = text.strip()
    
    # Remove isolated special characters that are likely noise
    text = re.sub(r'^[|\\/_\-~`]+$', '', text)
    
    # Fix common OCR misreads
    text = text.replace('|', 'I')
    text = text.replace('}{', 'H')
    
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def parse_vehicle_data(raw_text: str) -> Dict[str, Any]:
    """
    Parse raw OCR text into structured vehicle data.
    
    The SUNARP image format has:
    - "DATOS DEL VEHICULO" header
    - Key-value pairs like "MARCA: MERCEDES BENZ"
    - "PROPIETARIO(S):" section at the bottom
    """
    result = {
        "placa": None,
        "serie": None,
        "vin": None,
        "motor": None,
        "color": None,
        "marca": None,
        "modelo": None,
        "placa_vigente": None,
        "placa_anterior": None,
        "estado": None,
        "anotaciones": None,
        "sede": None,
        "anio_modelo": None,
        "propietario": None,
        "raw_text": raw_text,
    }
    
    lines = raw_text.split('\n')
    
    # Track if we're in the propietario section
    in_propietario = False
    propietario_lines = []
    
    for line in lines:
        line = clean_text(line)
        if not line:
            continue
        
        # Check for propietario section
        if 'PROPIETARIO' in line.upper():
            in_propietario = True
            # Check if there's content after the colon
            if ':' in line:
                after_colon = line.split(':', 1)[1].strip()
                if after_colon:
                    propietario_lines.append(after_colon)
            continue
        
        if in_propietario:
            # Skip timestamp line at bottom
            if re.match(r'^\d{2}/\d{2}/\d{4}', line):
                continue
            propietario_lines.append(line)
            continue
        
        # Try to match key:value pattern
        if ':' in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                key = parts[0].strip().upper()
                value = parts[1].strip()
                
                # Clean up key - remove accidental characters but keep * and letters
                key = re.sub(r'[^A-ZÀ-ÿ\s°º*]', '', key).strip()
                
                # Try to match the key - use longest match first
                matched_field = None
                best_match_len = 0
                
                for pattern, field in FIELD_MAPPINGS.items():
                    pattern_upper = pattern.upper()
                    
                    # Exact match
                    if key == pattern_upper:
                        matched_field = field
                        break
                    
                    # Check if pattern is contained in key (for longer patterns)
                    if pattern_upper in key and len(pattern_upper) > best_match_len:
                        matched_field = field
                        best_match_len = len(pattern_upper)
                    
                    # Check if key is contained in pattern (for partial OCR reads)
                    elif key in pattern_upper and len(key) >= 4 and len(key) > best_match_len:
                        matched_field = field
                        best_match_len = len(key)
                
                if matched_field and value:
                    # Clean up value
                    value = value.strip()
                    # Remove leading dashes or special chars
                    value = re.sub(r'^[\-—–]+\s*', '', value)
                    result[matched_field] = value
    
    # Set propietario
    if propietario_lines:
        result["propietario"] = ' '.join(propietario_lines).strip()
    
    return result


def extract_vehicle_data(image_path: str) -> Dict[str, Any]:
    """
    Main function to extract and parse vehicle data from SUNARP image.
    
    Args:
        image_path: Path to the SUNARP vehicle image
        
    Returns:
        Dictionary with extracted vehicle data
    """
    # Verify file exists
    path = Path(image_path)
    if not path.exists():
        return {
            "success": False,
            "error": f"Image file not found: {image_path}",
            "data": None,
        }
    
    try:
        # Extract raw text
        raw_text = extract_text_raw(image_path, preprocess=True)
        
        # If raw text is too short, try aggressive preprocessing
        if len(raw_text.strip()) < 50:
            processed = preprocess_image_aggressive(image_path)
            pil_image = Image.fromarray(processed)
            config = r'--oem 3 --psm 6'
            raw_text = pytesseract.image_to_string(pil_image, config=config)
        
        # Parse the text into structured data
        data = parse_vehicle_data(raw_text)
        
        # Check if we got meaningful data
        has_data = any(v for k, v in data.items() if k != 'raw_text' and v is not None)
        
        return {
            "success": has_data,
            "error": None if has_data else "Could not extract meaningful data",
            "data": data,
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "data": None,
        }


def extract_vehicle_data_debug(image_path: str) -> Dict[str, Any]:
    """
    Debug version that includes intermediate processing results.
    """
    path = Path(image_path)
    if not path.exists():
        return {"error": f"Image file not found: {image_path}"}
    
    results = {
        "image_path": str(path),
        "methods": {}
    }
    
    # Method 1: Standard preprocessing
    try:
        raw_text_1 = extract_text_raw(image_path, preprocess=True)
        data_1 = parse_vehicle_data(raw_text_1)
        results["methods"]["standard"] = {
            "raw_text": raw_text_1,
            "parsed": data_1
        }
    except Exception as e:
        results["methods"]["standard"] = {"error": str(e)}
    
    # Method 2: Aggressive preprocessing
    try:
        processed = preprocess_image_aggressive(image_path)
        pil_image = Image.fromarray(processed)
        config = r'--oem 3 --psm 6'
        raw_text_2 = pytesseract.image_to_string(pil_image, config=config)
        data_2 = parse_vehicle_data(raw_text_2)
        results["methods"]["aggressive"] = {
            "raw_text": raw_text_2,
            "parsed": data_2
        }
    except Exception as e:
        results["methods"]["aggressive"] = {"error": str(e)}
    
    # Method 3: No preprocessing (original image)
    try:
        raw_text_3 = extract_text_raw(image_path, preprocess=False)
        data_3 = parse_vehicle_data(raw_text_3)
        results["methods"]["raw"] = {
            "raw_text": raw_text_3,
            "parsed": data_3
        }
    except Exception as e:
        results["methods"]["raw"] = {"error": str(e)}
    
    # Select best result (most non-null fields)
    best_method = None
    best_count = 0
    
    for method_name, method_result in results["methods"].items():
        if "parsed" in method_result:
            count = sum(1 for k, v in method_result["parsed"].items() 
                       if k != 'raw_text' and v is not None)
            if count > best_count:
                best_count = count
                best_method = method_name
    
    results["best_method"] = best_method
    if best_method:
        results["best_result"] = results["methods"][best_method]["parsed"]
    
    return results


# CLI for testing
if __name__ == "__main__":
    import sys
    import json
    
    if len(sys.argv) < 2:
        print("Usage: python ocr.py <image_path> [--debug]")
        print("\nExample:")
        print("  python ocr.py downloads/sunarp_ABC123_20260406.png")
        print("  python ocr.py downloads/sunarp_ABC123_20260406.png --debug")
        sys.exit(1)
    
    image_path = sys.argv[1]
    debug_mode = "--debug" in sys.argv
    
    print(f"Processing: {image_path}")
    print("-" * 50)
    
    if debug_mode:
        result = extract_vehicle_data_debug(image_path)
    else:
        result = extract_vehicle_data(image_path)
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
