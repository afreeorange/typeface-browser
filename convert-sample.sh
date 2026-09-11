#!/bin/bash

# Font to CSS converter script
# Converts WOFF2 fonts to @font-face declarations with base64 data URLs

# Output CSS file
OUTPUT_FILE="circular-std-fonts.css"

# Clear the output file
> "$OUTPUT_FILE"

# Function to determine font weight from filename
get_font_weight() {
    local filename="$1"
    case "$filename" in
        *"Light"*) echo "300" ;;
        *"Book"*) echo "400" ;;
        *"Medium"*) echo "500" ;;
        *"Bold"*) echo "700" ;;
        *"Black"*) echo "900" ;;
        *) echo "400" ;;
    esac
}

# Function to determine font style from filename
get_font_style() {
    local filename="$1"
    if [[ "$filename" == *"Italic"* ]]; then
        echo "italic"
    else
        echo "normal"
    fi
}

# Array of font files
fonts=(
    "CircularStd-Black.woff2"
    "CircularStd-BlackItalic.woff2"
    "CircularStd-Bold.woff2"
    "CircularStd-BoldItalic.woff2"
    "CircularStd-Book.woff2"
    "CircularStd-BookItalic.woff2"
    "CircularStd-Light Italic.woff2"
    "CircularStd-Light.woff2"
    "CircularStd-Medium.woff2"
    "CircularStd-MediumItalic.woff2"
)

echo "Converting fonts to CSS with base64 encoding..."
echo "/* Circular Standard Font Family */" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Process each font file
for font in "${fonts[@]}"; do
    if [[ -f "$font" ]]; then
        echo "Processing: $font"

        # Get font properties
        weight=$(get_font_weight "$font")
        style=$(get_font_style "$font")

        # Convert font to base64
        base64_data=$(base64 -i "$font")

        # Generate @font-face declaration
        cat >> "$OUTPUT_FILE" << EOF
@font-face {
    font-family: 'Circular Std';
    src: url('data:font/woff2;base64,$base64_data') format('woff2');
    font-weight: $weight;
    font-style: $style;
    font-display: swap;
}

EOF
        echo "✓ Converted: $font (weight: $weight, style: $style)"
    else
        echo "⚠ File not found: $font"
    fi
done

echo ""
echo "Conversion complete! CSS saved to: $OUTPUT_FILE"
echo ""
echo "Usage in your CSS:"
echo "body { font-family: 'Circular Std', sans-serif; }"
echo ""
echo "Available weights: 300 (Light), 400 (Book), 500 (Medium), 700 (Bold), 900 (Black)"
echo "Available styles: normal, italic"
