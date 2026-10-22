# Android slot: emulator (x86_64, KVM), Xvfb, x11vnc + noVNC, the Android tool server and slotd.
# Build from the repo root (needs android-toolserver/): docker build -f android-slot/Dockerfile -t android-slot:local .
# deploy.sh does this, then makes the golden snapshot with init-snapshot.sh on the host that has /dev/kvm.
FROM debian:bookworm-slim

ARG ANDROID_API=34
# pinned; set CMDLINE_TOOLS_SHA256 (Google publishes it next to the download) to verify the archive too
ARG CMDLINE_TOOLS_URL=https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
ARG CMDLINE_TOOLS_SHA256=
# F-Droid APK: a VERSIONED url and its sha256 (the unversioned F-Droid.apk changes with each release)
ARG FDROID_URL
ARG FDROID_SHA256

ENV ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_API=${ANDROID_API} \
    PATH=/venv/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates curl unzip tini iptables iproute2 util-linux netcat-openbsd procps \
      openjdk-17-jre-headless xvfb x11vnc novnc websockify \
      libpulse0 libnss3 libxcomposite1 libxcursor1 libxi6 libxtst6 libxkbcommon-x11-0 libgl1 libasound2 \
      libdbus-1-3 libxdamage1 libxrandr2 libxkbfile1 libxss1 libxshmfence1 libxfixes3 libxext6 libx11-xcb1 \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL -o /tmp/clt.zip "$CMDLINE_TOOLS_URL"; \
    if [ -n "$CMDLINE_TOOLS_SHA256" ]; then echo "$CMDLINE_TOOLS_SHA256  /tmp/clt.zip" | sha256sum -c -; fi; \
    mkdir -p /opt/android-sdk/cmdline-tools; \
    unzip -q /tmp/clt.zip -d /opt/android-sdk/cmdline-tools; \
    mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest; \
    rm /tmp/clt.zip; \
    yes | sdkmanager --licenses >/dev/null; \
    sdkmanager "platform-tools" "emulator" "system-images;android-${ANDROID_API};google_apis_playstore;x86_64"

RUN set -eux; \
    : "${FDROID_URL:?set --build-arg FDROID_URL}" "${FDROID_SHA256:?set --build-arg FDROID_SHA256}"; \
    curl -fsSL -o /opt/fdroid.apk "$FDROID_URL"; \
    echo "$FDROID_SHA256  /opt/fdroid.apk" | sha256sum -c -

# emu: the emulator (and nothing else); svc: tool server, adb server, VNC. Distinct uids so the firewall can tell
# the guest's traffic from the infrastructure's.
RUN useradd -u 2000 -m -d /home/emu -s /usr/sbin/nologin emu \
 && useradd -u 3000 -m -d /home/svc -s /usr/sbin/nologin svc \
 && mkdir -p /opt/adb /avd /out /in /opt/toolserver \
 && adb keygen /opt/adb/adbkey && chmod 644 /opt/adb/adbkey.pub && chmod 600 /opt/adb/adbkey

RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir "mcp==1.29.1" "uiautomator2>=3,<4" uvicorn

COPY android-toolserver/android_ui.py android-toolserver/android_tools.py android-toolserver/device.py android-toolserver/android_server.py /opt/toolserver/
COPY android-slot/entrypoint.sh android-slot/tor-route.sh android-slot/firewall.sh android-slot/prep-homes.sh android-slot/init-snapshot.sh android-slot/slotd.py android-slot/console.py /app/
RUN chmod +x /app/*.sh

EXPOSE 9000 9001 6080
ENTRYPOINT ["/app/entrypoint.sh"]
