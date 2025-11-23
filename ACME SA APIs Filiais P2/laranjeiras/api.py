from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, Body
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import uvicorn
import requests
import json
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

API_NAME = "Laranjeiras ACME/SA API"
API_PORT = int(os.getenv('API_PORT', 8002))
DATABASE_NAME = os.getenv('DATABASE_NAME', 'laranjeiras.db')

app = FastAPI(title=API_NAME, version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REPLICAS = load_replicas('laranjeiras')

replica_manager = ReplicaManager("laranjeiras", REPLICAS)

@app.on_event("startup")
async def startup_event():
    init_database(DATABASE_NAME, API_NAME)
    
    matriz_url = REPLICAS.get('matriz')
    if matriz_url:
        try:
            token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(f"{matriz_url}/produtos", headers=headers, timeout=5)
            
            if response.status_code == 200:
                produtos_matriz = response.json()
                conn = get_db_connection(DATABASE_NAME)
                cursor = conn.cursor()
                
                for produto in produtos_matriz:
                    try:
                        cursor.execute(
                            "SELECT id FROM produtos WHERE codigo = ?",
                            (produto['codigo'],)
                        )
                        produto_local = cursor.fetchone()
                        
                        if not produto_local:
                            cursor.execute(
                                "INSERT INTO produtos (codigo, nome, preco) VALUES (?, ?, ?)",
                                (produto['codigo'], produto['nome'], produto['preco'])
                            )
                            produto_id_local = cursor.lastrowid
                            
                            resp_estoque = requests.get(f"{matriz_url}/estoque/{produto['codigo']}", headers=headers, timeout=3)
                            quantidade_matriz = 0
                            if resp_estoque.status_code == 200:
                                quantidade_matriz = resp_estoque.json().get('quantidade', 0)
                            
                            cursor.execute(
                                "INSERT INTO estoque (produto_id, quantidade) VALUES (?, ?)",
                                (produto_id_local, quantidade_matriz)
                            )
                        else:
                            resp_estoque = requests.get(f"{matriz_url}/estoque/{produto['codigo']}", headers=headers, timeout=3)
                            if resp_estoque.status_code == 200:
                                quantidade_matriz = resp_estoque.json().get('quantidade', 0)
                                cursor.execute(
                                    "UPDATE estoque SET quantidade = ? WHERE produto_id = ?",
                                    (quantidade_matriz, produto_local['id'])
                                )
                    except:
                        pass
                
                conn.commit()
                conn.close()
        except:
            pass

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

@app.post("/produtos", tags=["Produtos"])
async def criar_produto(
    request: Request,
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
        cursor.execute(
            "SELECT * FROM produtos WHERE codigo = ?",
            (codigo,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Código de produto já existe")
        
        if not origem:
            token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {token}"}
            data = {
                "codigo": codigo,
                "nome": nome,
                "preco": preco,
                "quantidade": quantidade,
                "origem": API_NAME
            }
            
            matriz_url = REPLICAS.get('matriz')
            if matriz_url:
                try:
                    resp = requests.post(
                        f"{matriz_url}/produtos",
                        data=data,
                        headers=headers,
                        timeout=5
                    )
                    resp.raise_for_status() 
                
                except requests.Timeout:
                    raise HTTPException(status_code=504, detail="Matriz demorou para responder (timeout)")
                except requests.HTTPError as e:
                    detail = f"Matriz falhou: {e.response.text}"
                    try:
                        detail_json = e.response.json().get('detail')
                        if detail_json:
                            detail = f"Matriz recusou: {detail_json}"
                    except:
                        pass
                    raise HTTPException(status_code=e.response.status_code, detail=detail)
                except requests.RequestException as e:
                    raise HTTPException(status_code=503, detail=f"Erro de rede ao contatar matriz: {str(e)}")

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
        return {
            "message": "Produto criado com sucesso"
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/pedidos", tags=["Pedidos"])
async def listar_pedidos(
    current_user: dict = Depends(get_current_user)
):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT id, total, criado_em FROM pedidos ORDER BY criado_em DESC"
    )
    pedidos = cursor.fetchall()
    
    resultado = []
    for pedido in pedidos:
        cursor.execute(
            "SELECT pi.quantidade, pi.preco_unitario, pi.subtotal, p.id as produto_id, p.codigo, p.nome FROM pedidos_itens pi JOIN produtos p ON pi.produto_id = p.id WHERE pi.pedido_id = ?",
            (pedido['id'],)
        )
        itens = cursor.fetchall()
        
        resultado.append({
            "id": pedido['id'],
            "total": pedido['total'],
            "criado_em": pedido['criado_em'],
            "itens": [
                {
                    "produto_id": item['produto_id'],
                    "produto_codigo": item['codigo'],
                    "produto_nome": item['nome'],
                    "quantidade": item['quantidade'],
                    "preco_unitario": item['preco_unitario'],
                    "subtotal": item['subtotal']
                }
                for item in itens
            ]
        })
    
    conn.close()
    return resultado

@app.get("/pedido/{pedido_id}", tags=["Pedidos"])
async def consultar_pedido(
    pedido_id: int,
    current_user: dict = Depends(get_current_user)
):
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT id, total, criado_em FROM pedidos WHERE id = ?",
        (pedido_id,)
    )
    pedido = cursor.fetchone()
    
    if not pedido:
        conn.close()
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    
    cursor.execute(
        "SELECT pi.quantidade, pi.preco_unitario, pi.subtotal, p.id as produto_id, p.codigo, p.nome FROM pedidos_itens pi JOIN produtos p ON pi.produto_id = p.id WHERE pi.pedido_id = ?",
        (pedido_id,)
    )
    itens = cursor.fetchall()
    conn.close()
    
    return {
        "id": pedido['id'],
        "total": pedido['total'],
        "criado_em": pedido['criado_em'],
        "itens": [
            {
                "produto_id": item['produto_id'],
                "produto_codigo": item['codigo'],
                "produto_nome": item['nome'],
                "quantidade": item['quantidade'],
                "preco_unitario": item['preco_unitario'],
                "subtotal": item['subtotal']
            }
            for item in itens
        ]
    }

@app.post("/pedido", tags=["Pedidos"])
async def criar_pedido(
    pedido: dict = Body(example={"itens": [{"codigo_produto": "123", "quantidade": 5}]}),
    current_user: dict = Depends(get_current_user)
):
    itens = pedido.get('itens', [])
    
    if not isinstance(itens, list):
        raise HTTPException(status_code=400, detail="Formato inválido para itens")
    
    if not itens:
        raise HTTPException(status_code=400, detail="Pedido deve conter ao menos um item")
    
    conn = get_db_connection(DATABASE_NAME)
    cursor = conn.cursor()
    
    try:
        conn.execute("BEGIN EXCLUSIVE")
        
        total_pedido = 0
        itens_validados = []
        
        for item in itens:
            codigo_produto = item.get('codigo_produto')
            quantidade = item.get('quantidade', 0)
            
            if not codigo_produto:
                raise HTTPException(status_code=400, detail="Item do pedido não contém 'codigo_produto'")
            
            cursor.execute(
                "SELECT p.id, p.codigo, p.nome, p.preco, e.quantidade FROM produtos p JOIN estoque e ON p.id = e.produto_id WHERE p.codigo = ?",
                (codigo_produto,)
            )
            produto = cursor.fetchone()
            
            if not produto:
                raise HTTPException(status_code=404, detail=f"Produto com código {codigo_produto} não encontrado")
            
            if produto['quantidade'] < quantidade:
                raise HTTPException(
                    status_code=400,
                    detail=f"Estoque insuficiente para {produto['nome']}. Disponível: {produto['quantidade']}"
                )
            
            subtotal = quantidade * produto['preco']
            total_pedido += subtotal
            
            itens_validados.append({
                'produto_id': produto['id'],
                'produto_codigo': produto['codigo'],
                'produto_nome': produto['nome'],
                'quantidade': quantidade,
                'preco_unitario': produto['preco'],
                'subtotal': subtotal
            })
        
        cursor.execute(
            "INSERT INTO pedidos (total) VALUES (?)",
            (total_pedido,)
        )
        pedido_id = cursor.lastrowid
        
        token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
        headers = {"Authorization": f"Bearer {token}"}
        matriz_url = REPLICAS.get('matriz')
        
        for item in itens_validados:
            cursor.execute(
                "INSERT INTO pedidos_itens (pedido_id, produto_id, quantidade, preco_unitario, subtotal) VALUES (?, ?, ?, ?, ?)",
                (pedido_id, item['produto_id'], item['quantidade'], item['preco_unitario'], item['subtotal'])
            )
            
            cursor.execute(
                "UPDATE estoque SET quantidade = quantidade - ?, atualizado_em = CURRENT_TIMESTAMP WHERE produto_id = ?",
                (item['quantidade'], item['produto_id'])
            )
            
            if matriz_url:
                try:
                    data = {
                        "operacao": "saida",
                        "quantidade": item['quantidade'],
                        "origem": API_NAME
                    }
                    resp_put = requests.put(
                        f"{matriz_url}/estoque/{item['produto_codigo']}",
                        data=data,
                        headers=headers,
                        timeout=5
                    )
                    resp_put.raise_for_status()

                except requests.HTTPError as e:
                    detail = f"Matriz recusou baixa de estoque: {e.response.text}"
                    try:
                        detail_json = e.response.json().get('detail')
                        if detail_json:
                            detail = f"Matriz recusou: {detail_json}"
                    except:
                        pass
                    raise HTTPException(status_code=e.response.status_code, detail=detail)
                
                except Exception as e:
                    raise HTTPException(status_code=503, detail=f"Erro de rede ao atualizar estoque na matriz: {str(e)}")
        
        conn.commit()
        
        return {
            "message": "Pedido criado com sucesso",
            "pedido_id": pedido_id,
            "total": total_pedido,
            "itens": itens_validados
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

@app.put("/estoque/{codigo_produto}", tags=["Estoque"])
async def atualizar_estoque(
    request: Request,
    codigo_produto: str,
    current_user: dict = Depends(require_admin),
    operacao: str = Form(default="entrada"),
    quantidade: int = Form(default=10)
):
    form_data = await request.form()
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
                raise HTTPException(
                    status_code=400,
                    detail=f"Estoque insuficiente. Disponível: {quantidade_anterior}"
                )
            nova_quantidade = quantidade_anterior - quantidade
        
        if not origem:
            token = create_access_token(data={"sub": "admin"}, expires_delta=timedelta(minutes=5))
            headers = {"Authorization": f"Bearer {token}"}
            data = {
                "operacao": operacao,
                "quantidade": quantidade,
                "origem": API_NAME
            }
            
            matriz_url = REPLICAS.get('matriz')
            if matriz_url:
                try:
                    resp_put = requests.put(
                        f"{matriz_url}/estoque/{codigo_produto}",
                        data=data,
                        headers=headers,
                        timeout=5
                    )
                    resp_put.raise_for_status()
                except requests.Timeout:
                    raise HTTPException(status_code=504, detail="Matriz demorou para responder (timeout)")
                except requests.HTTPError as e:
                    detail = f"Matriz falhou: {e.response.text}"
                    try:
                        detail_json = e.response.json().get('detail')
                        if detail_json:
                            detail = f"Matriz recusou: {detail_json}"
                    except:
                        pass
                    raise HTTPException(status_code=e.response.status_code, detail=detail)
                except requests.RequestException as e:
                    raise HTTPException(status_code=503, detail=f"Erro de rede ao contatar matriz: {str(e)}")

        cursor.execute(
            "UPDATE estoque SET quantidade = ?, atualizado_em = CURRENT_TIMESTAMP WHERE produto_id = ?",
            (nova_quantidade, produto_id_local)
        )
        
        conn.commit()
        
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