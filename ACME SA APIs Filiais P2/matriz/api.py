from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import uvicorn
import requests
from datetime import datetime, timedelta
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.database import init_database, get_db_connection
from shared.auth import (
    create_access_token, get_current_user, require_admin,
    verify_password, ACCESS_TOKEN_EXPIRE_MINUTES
)
from shared.sync import ReplicaManager, load_replicas

load_dotenv('.env')

API_NAME = "Matriz ACME/SA API"
API_PORT = int(os.getenv('API_PORT', 8000))
DATABASE_NAME = os.getenv('DATABASE_NAME', 'matriz.db')

app = FastAPI(title=API_NAME, version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REPLICAS = load_replicas('matriz')

replica_manager = ReplicaManager("matriz", REPLICAS)

def replicar_produto_sync(data: dict, headers: dict, skip_origem: str):
    for nome_filial, url_filial in REPLICAS.items():
        if skip_origem and nome_filial.lower() in skip_origem.lower():
            continue
        try:
            requests.post(
                f"{url_filial}/produtos",
                data=data,
                headers=headers,
                timeout=5
            )
        except Exception as e:
            print(f"ERRO: Falha ao replicar produto {data.get('codigo')} para {nome_filial}: {e}")

def replicar_estoque_sync(codigo_produto: str, data: dict, headers: dict, skip_origem: str):
    for nome_filial, url_filial in REPLICAS.items():
        if skip_origem and nome_filial.lower() in skip_origem.lower():
            continue
        try:
            requests.put(
                f"{url_filial}/estoque/{codigo_produto}",
                data=data,
                headers=headers,
                timeout=5
            )
        except Exception as e:
            print(f"ERRO: Falha ao replicar estoque {codigo_produto} para {nome_filial}: {e}")

@app.on_event("startup")
async def startup_event():
    init_database(DATABASE_NAME, API_NAME)

@app.post("/login", include_in_schema=False)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT * FROM usuarios WHERE login = ?",
        (form_data.username,)
    )
    user_data = cursor.fetchone()
    conn.close()
    
    if not user_data or not verify_password(form_data.password, user_data['password']):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuário ou senha incorretos"
        )
    
    access_token = create_access_token(
        data={"sub": user_data['login']},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/usuarios", tags=["Usuários"])
async def criar_usuario(
    login: str = Form(default="teste"),
    password: str = Form(default="teste123"),
    current_user: dict = Depends(require_admin)
):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT * FROM usuarios WHERE login = ?",
            (login,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Login já existe")
        
        cursor.execute(
            "INSERT INTO usuarios (login, password) VALUES (?, ?)",
            (login, password)
        )
        conn.commit()
        
        return {
            "message": "Usuário criado com sucesso"
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        conn.close()

@app.get("/produtos", tags=["Produtos"])
async def listar_produtos(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, codigo, nome, preco, criado_em FROM produtos")
    produtos = cursor.fetchall()
    conn.close()
    
    return [
        {
            "id": produto['id'],
            "codigo": produto['codigo'],
            "nome": produto['nome'],
            "preco": produto['preco'],
            "criado_em": produto['criado_em']
        }
        for produto in produtos
    ]

@app.post("/produtos", include_in_schema=False)
async def criar_produto(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin),
    codigo: str = Form(default="123"),
    nome: str = Form(default="mesa"),
    preco: float = Form(default=10.0),
    quantidade: int = Form(default=100)
):
    form_data = await request.form()
    origem = form_data.get('origem', None)
    
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    try:
        conn.execute("BEGIN EXCLUSIVE")
        cursor.execute(
            "SELECT * FROM produtos WHERE codigo = ?",
            (codigo,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Código de produto já existe")
        
        cursor.execute(
            "INSERT INTO produtos (codigo, nome, preco) VALUES (?, ?, ?)",
            (codigo, nome, preco)
        )
        produto_id = cursor.lastrowid
        
        cursor.execute(
            "INSERT INTO estoque (produto_id, quantidade) VALUES (?, ?)",
            (produto_id, quantidade)
        )
        
        conn.commit()
        
        token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
        headers = {"Authorization": f"Bearer {token}"}
        
        data_para_replicar = {
            "codigo": codigo,
            "nome": nome,
            "preco": preco,
            "quantidade": quantidade,
            "origem": "matriz"
        }
        
        background_tasks.add_task(
            replicar_produto_sync,
            data=data_para_replicar,
            headers=headers,
            skip_origem=origem
        )
        
        return {
            "message": "Produto criado",
            "id": produto_id,
            "codigo": codigo
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/estoque/{codigo_produto}", tags=["Estoque"])
async def consultar_estoque(
    codigo_produto: str,
    current_user: dict = Depends(get_current_user)
):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT p.id, p.codigo, p.nome, e.quantidade, e.atualizado_em FROM produtos p JOIN estoque e ON p.id = e.produto_id WHERE p.codigo = ?",
        (codigo_produto,)
    )
    resultado = cursor.fetchone()
    conn.close()
    
    if not resultado:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    
    return {
        "produto_id": resultado['id'],
        "produto_codigo": resultado['codigo'],
        "produto_nome": resultado['nome'],
        "quantidade": resultado['quantidade'],
        "atualizado_em": resultado['atualizado_em']
    }

@app.put("/estoque/{codigo_produto}", include_in_schema=False)
async def atualizar_estoque(
    request: Request,
    codigo_produto: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    form_data = await request.form()
    operacao = form_data.get('operacao', 'entrada')
    quantidade = int(form_data.get('quantidade', 0))
    origem = form_data.get('origem', None)
    
    if operacao not in ['entrada', 'saida']:
        raise HTTPException(status_code=400, detail="Operação inválida. Use 'entrada' ou 'saida'")
    
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    try:
        conn.execute("BEGIN EXCLUSIVE")
        
        cursor.execute(
            "SELECT p.id, e.quantidade FROM produtos p JOIN estoque e ON p.id = e.produto_id WHERE p.codigo = ?",
            (codigo_produto,)
        )
        produto = cursor.fetchone()
        
        if not produto:
            raise HTTPException(status_code=404, detail="Produto não encontrado")
        
        produto_id_local = produto['id']
        quantidade_anterior = produto['quantidade']
        
        if operacao == "entrada":
            nova_quantidade = quantidade_anterior + quantidade
        else:
            if quantidade_anterior < quantidade:
                raise HTTPException(status_code=400, detail=f"Estoque insuficiente. Disponível: {quantidade_anterior}")
            nova_quantidade = quantidade_anterior - quantidade
        
        cursor.execute(
            "UPDATE estoque SET quantidade = ?, atualizado_em = CURRENT_TIMESTAMP WHERE produto_id = ?",
            (nova_quantidade, produto_id_local)
        )
        
        conn.commit()
        
        token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
        headers = {"Authorization": f"Bearer {token}"}
        
        data_para_replicar = {
            "operacao": operacao,
            "quantidade": quantidade,
            "origem": "matriz"
        }
        
        background_tasks.add_task(
            replicar_estoque_sync,
            codigo_produto=codigo_produto,
            data=data_para_replicar,
            headers=headers,
            skip_origem=origem
        )
        
        return {
            "message": "Estoque atualizado",
            "produto_id": produto_id_local,
            "codigo_produto": codigo_produto,
            "operacao": operacao,
            "quantidade_alterada": quantidade,
            "quantidade_anterior": quantidade_anterior,
            "quantidade_atual": nova_quantidade
        }
        
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/status", tags=["Filiais"])
async def get_status(current_user: dict = Depends(get_current_user)):
    replicas_status = await replica_manager.check_all_replicas()
    
    return {
        "api_name": API_NAME,
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "replicas": replicas_status
    }

if __name__ == "__main__":
    uvicorn.run("api:app", host="localhost", port=API_PORT, reload=True)