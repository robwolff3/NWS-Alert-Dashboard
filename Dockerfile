FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    cmake \
    build-essential \
    libsndfile1-dev \
    libportaudio2 \
    libusb-1.0-0 \
    sox \
    curl \
    git \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install rtl-sdr 0.6.0-3 from Debian bullseye archive rather than bookworm's
# 2.0.2 (RTL-SDR Blog fork), which silently ignores the -E deemp flag.
RUN curl -L -o /tmp/librtlsdr0.deb \
      "http://archive.debian.org/debian/pool/main/r/rtl-sdr/librtlsdr0_0.6.0-3_amd64.deb" \
    && curl -L -o /tmp/rtl-sdr.deb \
      "http://archive.debian.org/debian/pool/main/r/rtl-sdr/rtl-sdr_0.6.0-3_amd64.deb" \
    && dpkg -i /tmp/librtlsdr0.deb /tmp/rtl-sdr.deb \
    && rm /tmp/librtlsdr0.deb /tmp/rtl-sdr.deb

# Build multimon-ng from source — without libpulse-dev present,
# CMake disables PulseAudio support and it reads cleanly from stdin
RUN git clone https://github.com/EliasOenal/multimon-ng.git /tmp/multimon-ng \
    && cmake -S /tmp/multimon-ng -B /tmp/multimon-ng/build \
    && make -C /tmp/multimon-ng/build -j$(nproc) \
    && cp /tmp/multimon-ng/build/multimon-ng /usr/local/bin/ \
    && rm -rf /tmp/multimon-ng

WORKDIR /app

RUN git clone https://github.com/jamieden/dsame3.git /app/dsame3

# faster_whisper is not installed; make its import non-fatal
RUN python3 -c "p='/app/dsame3/dsame.py'; s=open(p).read(); s=s.replace('from faster_whisper import WhisperModel','try:\n    from faster_whisper import WhisperModel\nexcept ImportError:\n    WhisperModel = None'); open(p,'w').write(s)"

# The upstream source_process loop uses `while True: readline()` with no EOF
# break, spinning at 100% CPU when the pipe goes quiet. Replace with iter()
# which terminates cleanly on EOF (readline returns b'').
RUN python3 -c "p='/app/dsame3/dsame.py'; s=open(p).read(); old='        while True:\n            line = source_process.stdout.readline()\n            if line:\n'; new='        for line in iter(source_process.stdout.readline, b\"\"):\n            if line:\n'; assert old in s; open(p,'w').write(s.replace(old,new))"

# The stdin path also wraps `for line in sys.stdin` in `while True:`, which
# spins at 100% CPU after the upstream pipe closes. Remove the outer loop so
# dsame3 exits cleanly on EOF and run.sh restarts the full pipeline.
RUN python3 -c "p='/app/dsame3/dsame.py'; s=open(p).read(); old='    else:\n        while True:\n            for line in sys.stdin:\n                logging.debug(line)\n                same_decode(line, args.lang, same_watch=args.same, event_watch=args.event, text=args.text,\n                            call=args.call, command=args.command, jsonfile=args.json)\n'; new='    else:\n        for line in sys.stdin:\n            logging.debug(line)\n            same_decode(line, args.lang, same_watch=args.same, event_watch=args.event, text=args.text,\n                        call=args.call, command=args.command, jsonfile=args.json)\n'; assert old in s, 'patch target not found'; open(p,'w').write(s.replace(old,new))"

# --source defaults to 'soundcard', which makes dsame3 spawn its own
# multimon-ng process and ignore stdin. Patch the default to None so that
# when --source is not provided, dsame3 falls through to the stdin branch
# and reads decoded EAS lines from the pipeline instead.
RUN python3 -c "p='/app/dsame3/dsame.py'; s=open(p).read(); old=\"parser.add_argument('--source', default='soundcard', choices=['rtl', 'soundcard', 'file']\"; new=\"parser.add_argument('--source', default=None, choices=['rtl', 'soundcard', 'file']\"; assert old in s, 'patch target not found'; open(p,'w').write(s.replace(old,new))"

# dsame3 sets the Windows console title via os.system("title ..."); on Linux
# sh this just prints "sh: 1: title: not found". Neuter it to keep logs clean.
RUN python3 -c "p='/app/dsame3/dsame.py'; s=open(p).read(); s=s.replace('os.system(\"title \" + ', '# os.system(\"title \" + '); open(p,'w').write(s)"

# numpy/sounddevice/soundfile/tqdm are top-level imports in dsame3
RUN pip install --no-cache-dir \
    numpy \
    sounddevice \
    soundfile \
    tqdm \
    requests \
    flask \
    waitress \
    pywebpush \
    apprise \
    slixmpp \
    pillow \
    paho-mqtt

# Vendor Leaflet so the frontend works without internet
RUN mkdir -p /app/scripts/static/leaflet \
    && curl -fsSL https://unpkg.com/leaflet@1.9.4/dist/leaflet.js \
         -o /app/scripts/static/leaflet/leaflet.js \
    && curl -fsSL https://unpkg.com/leaflet@1.9.4/dist/leaflet.css \
         -o /app/scripts/static/leaflet/leaflet.css

COPY Required_Weekly_Test_NOAA.ogg /app/
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.py /app/scripts/*.sh

CMD ["/app/scripts/run.sh"]
