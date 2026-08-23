FROM eclipse-temurin:17-jre-jammy

LABEL maintainer="CICT Team"

WORKDIR /app

RUN groupadd --system --gid 10001 cict \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app cict \
    && mkdir -p /data/logs \
    && chown -R cict:cict /app /data/logs

COPY --chown=cict:cict app.jar /app/app.jar

COPY application.properties /etc/cict/application.properties

EXPOSE 9090

USER cict

ENTRYPOINT ["java", "-jar", "/app/app.jar", "--spring.config.additional-location=/etc/cict/application.properties"]
