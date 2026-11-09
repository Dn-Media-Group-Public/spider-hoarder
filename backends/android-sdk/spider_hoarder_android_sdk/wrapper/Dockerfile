FROM docker:27-cli
COPY backends/android-sdk/spider_hoarder_android_sdk/wrapper/reconcile.sh /reconcile.sh
RUN chmod +x /reconcile.sh
ENTRYPOINT ["/reconcile.sh"]
