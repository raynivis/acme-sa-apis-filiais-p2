# ACME SA APIs Filiais

## Instalação e execução

1. Instalar as bibliotecas do python que as APIs usam, use o comando abaixo em  “ACME SA APIs Filiais P2/”.
python -m pip install -r bibliotecas.txt

2. Altere os .env das APIs caso deseje (no envio do projeto os .env das APIs estão criados), o modelo é:
API_PORT=8000
DATABASE_NAME=matriz.db

3. Ligar a matriz (a matriz estar ligada é obrigatória em um sistema distribuído com arquitetura cliente-servidor com topologia hub-and-spoke/estrela) em “ACME SA APIs Filiais P2/matriz”.
python api.py

4. Ligar as filias (pode se ligar uma, duas ou as três) em “ACME SA APIs Filiais P2/alipio”, “ACME SA APIs Filiais P2/alvorada” e “ACME SA APIs Filiais P2/laranjeiras”.
python api.py

5. Com a matriz ligada e as filiais que deseja, basta realizar as requisições pelo “url_da_api/docs/”

6. O login padrão de todas APIs é "admin" e senha "admin123"

## Arquitetura implementada

A arquitetura escolhida para o sistema da ACME/SA é baseada no modelo Cliente-Servidor, no qual a matriz atua como servidor central responsável por coordenar e manter a consistência dos dados entre as filiais, que funcionam como clientes, apesar de se familiarizar mais com uma topologia estrela ou “hub-and-spoke”.

Assim como nessas definições de arquitetura e topologia, o sistema possui o principal problema de ter um único ponto de falha, porque caso a matriz perca a conexão, a disponibilidade de todo sistema se perde, apesar de ter métodos de segurança e falhas.

## Estruturas de dados

Os dados criados nos bancos de dados de cada API são:

### Usuário
Feito para autenticação e login no sistema, ele é local (cada API tem seus usuários).  
O usuário possui os atributos de:
- id (identificador no banco de dados)  
- login (username para ter acesso às requisições)  
- password (senha para para ter acesso às requisições)  
- criado_em (data de criação daquele usuário)

### Produto
Feito para armazenar os produtos cadastrados no sistema, servindo como base para controle de estoque e pedidos, seus dados são distribuídos entre as réplicas.  
O produto possui os atributos de:
- id (identificador no banco de dados)  
- codigo (código único usado para identificar e replicar o produto entre as APIs)  
- nome (nome do produto)  
- preco (valor unitário do produto)  
- criado_em (data de criação daquele produto)

### Estoque
Feito para controlar a quantidade disponível de cada produto, garantindo o acompanhamento das entradas e saídas, seus dados são distribuídos entre as réplicas.  
O estoque possui os atributos de:
- id (identificador no banco de dados)  
- produto_id (referência ao produto que esse estoque pertence)  
- quantidade (quantidade atual daquele produto)  
- atualizado_em (data da última alteração da quantidade)

### Pedido
Feito para registrar vendas ou solicitações realizadas no sistema, armazenando o valor total e os dados principais da operação, ele é local (cada API tem seus pedidos).  
O pedido possui os atributos de:
- id (identificador no banco de dados)  
- total (valor total somado dos itens)  
- criado_em (data de criação daquele pedido)

### Pedidos_itens
Feito para registrar cada item pertencente a um pedido, armazenando suas quantidades, valores e referências. Cada item do produto tem o atributo de:
- id (identificador do item no banco)  
- pedido_id (referência ao pedido ao qual esse item pertence)  
- produto_id (referência ao produto adicionado no pedido)  
- quantidade (quantidade daquele produto no pedido)  
- preco_unitario (valor do produto)  
- subtotal (valor calculado multiplicando quantidade × preço unitário)

Os dados que são consistentes entre as réplicas são os produtos e estoque que são controlados pela matriz para a disponibilidade do recurso na hora de criar um pedido ou alterar o estoque, por exemplo.

## Requisições disponíveis nas réplicas

As requisições disponíveis nas réplicas são as mesmas, sendo elas:

- POST /login - para se autenticar no sistema  
- POST /usuarios - para criar um novo usuário de acesso  
- GET /produtos - retorna todos os produtos salvos no sistema distribuído  
- POST /produtos - cria um novo produto e replica para as outras filiais  
- GET /pedidos - retorna todos os pedidos daquela filial  
- GET /pedidos/{pedido_id} - retorna dados específicos de um pedido daquela filial  
- POST /pedidos - cria um novo pedido diminuindo o estoque de algum produto  
- GET /estoque/{codigo_produto} - retorna a quantidade e dados do produto no estoque entre as filiais  
- PUT /estoque/{codigo_produto} - dependendo da operação (“entrada” ou “saida”) atualiza o estoque do produto com aquele código  
- GET /status - retorna o status (online ou offline) das filiais e do servidor matriz  

Todas as requisições é necessário estar autenticado, exceto a de POST /login.

## Requisições da API matriz

A API matriz possui requisições mais limitadas, sendo elas:

- POST /login - para se autenticar no sistema  
- GET /produtos - retorna todos os produtos salvos no sistema distribuído  
- GET /estoque/{codigo_produto} - retorna a quantidade e dados do produto no estoque entre as filiais  
- GET /status - retorna o status (online ou offline) das filiais e do servidor matriz  

Todas as requisições é necessário estar autenticado, exceto a de POST /login, igual as filiais.

---

# 2. Estratégia de sincronização

A sincronização entre as filiais é realizada de duas formas e ambas dependem da matriz.

O primeiro caso de sincronização depende de a filial estar ligada na hora que alguma outra filial realiza uma criação de produto, ajuste de estoque ou pedido. A filial que realiza uma dessas operações avisa a matriz que deseja fazer essa ação. A matriz então, tendo a disponibilidade para realizar a operação, concede o feito no próprio banco de dados e replica os dados ajustados (de produto ou estoque) para as filiais que estiverem disponíveis naquele momento. Se ela perceber que tem alguma filial offline, ela apenas a ignora.

A matriz ignora a filial desligada porque existe a garantia de que, ao ligar, a API daquela filial vai se manter consistente com o resto do sistema. E isso nos dá o segundo caso de sincronização: quando a API de uma filial não está ligada no momento de uma dessas operações em outra filial. Ao ser ligada, a API da filial executa um comando para atualizar seu estoque e produtos com base nos dados salvos na matriz.

---

# 3. Estratégia de tolerância a falhas e segurança

Para garantir que o sistema funcione corretamente, mesmo com falhas, e que também seja seguro, foram implementadas algumas estratégias.

O controle da concorrência é resolvido pela matriz. Se duas filiais tentarem fazer um pedido ao mesmo tempo para o mesmo produto (por exemplo, as duas pedindo 100 unidades de um item que só tem 100 no estoque), a matriz garante que só um seja confirmado.

Ela usa uma trava (BEGIN EXCLUSIVE) no seu banco de dados (SQLite). A primeira requisição que chega é processada, o estoque é atualizado para 0 e o pedido é aprovado. A segunda requisição é forçada a esperar e, quando finalmente vai tentar, vê que o estoque já é 0, então a matriz recusa esse pedido. A filial que teve o pedido recusado (com um erro 400) cancela a operação localmente, garantindo que o estoque não fique negativo.

O sistema usa dois tipos de consistência:
- Para operações críticas, como um pedido ou uma baixa de estoque, é usada consistência forte: a filial deve esperar a matriz confirmar a operação antes de salvar localmente. Se a matriz negar (por falta de estoque, por exemplo), a filial cancela.  
- Já a replicação para as outras filiais (que não iniciaram a ação) usa consistência eventual, já que a matriz tenta avisar as outras filiais sobre um novo produto ou mudança de estoque, mas se elas estiverem offline, o sistema não para.

Em questão de segurança, o acesso de todas as rotas da API é bloqueado sem autenticação. Qualquer tentativa de acessar um endpoint sem um token de autorização JWT válido é imediatamente negado. Para ter acesso, o usuário precisa primeiro se autenticar na rota /login (enviando username e password), receber um token válido, e então enviar esse token no header de autorização em todas as próximas requisições.
