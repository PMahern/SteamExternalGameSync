@echo off
cd /d "%~dp0"
magick icon.png ^( -clone 0 -resize 256x256 ^) ^( -clone 0 -resize 64x64 ^) ^( -clone 0 -resize 32x32 ^) ^( -clone 0 -resize 16x16 ^) -delete 0 icon.ico
if errorlevel 1 ( echo [error] magick failed & exit /b 1 )
echo icon.ico written

rem Regenerate pre-launcher/icon_data.h
python -c "
data = open('icon.png','rb').read()
lines = ['  ' + ', '.join(f'0x{b:02x}' for b in data[i:i+12]) for i in range(0, len(data), 12)]
out = 'static const unsigned char icon_png[] = {\n' + ',\n'.join(lines) + '\n};\nstatic const unsigned int icon_png_len = ' + str(len(data)) + ';\n'
open('pre-launcher/icon_data.h','w').write(out)
"
if errorlevel 1 ( echo [error] python failed & exit /b 1 )
echo pre-launcher\icon_data.h written
