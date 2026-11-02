FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract.py scanner.py run.sh ./
RUN chmod a+x run.sh

EXPOSE 8099

CMD [ "/app/run.sh" ]
