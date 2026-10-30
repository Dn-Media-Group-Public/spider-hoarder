FROM docker:27-cli
COPY reconcile.sh /reconcile.sh
RUN chmod +x /reconcile.sh
ENTRYPOINT ["/reconcile.sh"]
