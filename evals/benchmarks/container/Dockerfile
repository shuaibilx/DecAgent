FROM decagent-install-test:py311
USER root
COPY wheelhouse /wheels
RUN python -m pip install --no-index --find-links=/wheels --no-deps \
    Jinja2==3.1.2 Werkzeug==2.3.8 itsdangerous==2.1.2 MarkupSafe==2.1.5 \
    attrs==23.2.0 packaging==23.2 py==1.11.0 chardet==3.0.4 urllib3==1.26.20 \
    && rm -rf /wheels
USER 65534:65534
