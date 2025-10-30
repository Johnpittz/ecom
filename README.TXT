Instruções rápidas:

1️⃣ Entre na pasta:
cd "C:\Users\E-commerce\Desktop\ecom_api"

2️⃣ Instale dependências:
pip install -r requirements.txt

3️⃣ Crie o arquivo .env com suas chaves:
   GOOGLE_API_KEY=...
   ML_PROXY_URL=...
   ML_PROXY_EXTRA=...

4️⃣ Rode o servidor:
uvicorn main:app --reload

5️⃣ Acesse no navegador:
http://127.0.0.1:8000/docs

6️⃣ Teste o endpoint:
GET /meli/search?q=iphone
