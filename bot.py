import os
import re
import logging
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import database as db

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN','')
GUILD_ID = int(os.getenv('GUILD_ID','0'))
REVIEWER_ROLE_ID = int(os.getenv('REVIEWER_ROLE_ID','0'))
OWNER_ROLE_ID = int(os.getenv('OWNER_ROLE_ID','0'))
CATEGORY_ID = int(os.getenv('CATEGORY_ID','0'))

logging.basicConfig(level=logging.INFO)


def is_staff(member):
    if not isinstance(member, discord.Member):
        return False
    return (member.guild_permissions.administrator or
            any(role.id in {REVIEWER_ROLE_ID,OWNER_ROLE_ID} and role.id != 0
                for role in member.roles))


def is_owner(member):
    if not isinstance(member,discord.Member):
        return False
    return (member.guild_permissions.administrator or
            any(role.id==OWNER_ROLE_ID and role.id!=0 for role in member.roles))


def profile_embed(member, record):
    balance, weekly, earned = db.point_summary(member.guild.id, member.id)
    em = discord.Embed(title='MCL | Профиль игрока', color=discord.Color.gold())
    em.add_field(name='Игровой ник',value=record['nickname'],inline=False)
    em.add_field(name='За неделю',value=f'{weekly} / {db.LIMIT}',inline=True)
    em.add_field(name='Баланс',value=str(balance),inline=True)
    em.add_field(name='Всего заработано',value=str(earned),inline=True)
    em.add_field(name='Неделя с',value=db.week_key(),inline=False)
    em.set_footer(text='Недельный счёт обновляется автоматически по понедельникам (Europe/Zagreb)')
    return em


def shop_embed(guild_id):
    items = db.shop_items(guild_id)
    lines = []
    for item in items:
        details = f' — {item["description"]}' if item['description'] else ''
        lines.append(f'**{item["name"]}** — `{item["price"]}` баллов{details}')
    description = '\n'.join(lines) if lines else 'Магазин пока не заполнен.'
    return discord.Embed(
        title='🛒 MCL | Магазин наград',
        description=description[:4096],
        color=discord.Color.gold(),
    )


def shop_panel_embed():
    return discord.Embed(
        title='🛒 MCL | Магазин наград',
        description=('Нажми **«Открыть магазин»**, выбери товар и подтверди запрос.\n'
                     'Запрос появится в твоём личном канале. Баллы спишутся только '
                     'после одобрения Owner / Tier Check.'),
        color=discord.Color.gold(),
    )


async def ensure_personal_channel(guild, user):
    """Create exactly one archive per player and reuse it on future visits."""
    record = db.player(guild.id, user.id)
    if not record:
        raise ValueError('Сначала зарегистрируйся.')
    cid = record['personal_channel_id']
    channel = guild.get_channel(cid) if cid else None
    if isinstance(channel, discord.TextChannel):
        return channel
    category = guild.get_channel(CATEGORY_ID)
    if not isinstance(category,discord.CategoryChannel):
        raise ValueError('CATEGORY_ID должен указывать на категорию Discord.')
    if not any(guild.get_role(r) for r in {REVIEWER_ROLE_ID,OWNER_ROLE_ID} - {0}):
        raise ValueError('Роли Owner / Tier Check не найдены. Проверь ID ролей в .env.')
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True,send_messages=True,
                        manage_channels=True,read_message_history=True,attach_files=True),
        user: discord.PermissionOverwrite(view_channel=True,send_messages=True,
                        read_message_history=True,attach_files=True)
    }
    for rid in {REVIEWER_ROLE_ID,OWNER_ROLE_ID} - {0}:
        role=guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True,
                         send_messages=True,read_message_history=True,attach_files=True)
    # Guild members can register only their own channel; no blanket channel permissions.
    channel=await guild.create_text_channel(
        f'откаты-{user.id}',category=category,overwrites=overwrites,
        reason=f'MCL личный архив: {user.id}')
    db.set_personal_channel(guild.id,user.id,channel.id)
    await channel.send(f'{user.mention}, это твой **постоянный архив откатов**. '
                       'Подавай заявки кнопкой «Подать на баллы» в главной панели, '
                       'затем загружай сюда фото/видео или ссылку. '
                       'Owner и Tier Check проверяют заявки прямо здесь.')
    return channel


class RegisterModal(discord.ui.Modal, title='MCL | Регистрация'):
    nickname=discord.ui.TextInput(label='Никнейм на Majestic',min_length=2,max_length=32,
                                   placeholder='Например: Sergey_MCL')

    async def on_submit(self,interaction:discord.Interaction):
        name=str(self.nickname).strip()
        if not re.fullmatch(r'[\w .-]{2,32}',name,flags=re.UNICODE):
            return await interaction.response.send_message('Ник может содержать буквы, цифры, пробел, _, точку и дефис.',ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True)
        db.register(interaction.guild_id,interaction.user.id,name)
        try:
            channel=await ensure_personal_channel(interaction.guild,interaction.user)
        except (discord.HTTPException,ValueError) as exc:
            logging.exception('Cannot create personal archive')
            return await interaction.followup.send(f'Профиль создан, но канал пока не создан: {exc}',ephemeral=True)
        await interaction.followup.send(f'✅ Профиль сохранён. Твой личный канал: {channel.mention}',ephemeral=True)


class ActivityModal(discord.ui.Modal,title='MCL | Заявка на баллы'):
    activity=discord.ui.TextInput(label='За какое мероприятие баллы?',
        style=discord.TextStyle.paragraph,placeholder='Капт, тренировка, мероприятие...',
        min_length=3,max_length=200)

    async def on_submit(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            channel=await ensure_personal_channel(interaction.guild,interaction.user)
        except (discord.HTTPException,ValueError) as exc:
            return await interaction.followup.send(str(exc),ephemeral=True)
        db.begin_upload(interaction.guild_id,interaction.user.id,str(self.activity))
        await interaction.followup.send(
            f'📎 Открой {channel.mention} и **в течение 30 минут** отправь одним сообщением '
            'скриншот, видео либо ссылку на откат. Бот создаст заявку с кнопками проверки. '
            'Файлы сохраняются в этом канале. Если видео слишком большое, прикрепи ссылку.',
            ephemeral=True)


class UnlimitedGrantModal(discord.ui.Modal,title='MCL | Выдать баллы без лимита'):
    amount=discord.ui.TextInput(label='Количество баллов',placeholder='Например: 500',
                                min_length=1,max_length=7)
    reason=discord.ui.TextInput(label='Причина',style=discord.TextStyle.paragraph,
                                placeholder='Старые баллы, приз, компенсация...',
                                min_length=3,max_length=250)

    def __init__(self,member_id,member_label):
        super().__init__()
        self.member_id=member_id
        self.member_label=member_label

    async def on_submit(self,interaction:discord.Interaction):
        if not is_owner(interaction.user):
            return await interaction.response.send_message('Только Owner / администратор.',ephemeral=True)
        try:
            amount=int(str(self.amount).strip())
        except ValueError:
            return await interaction.response.send_message('Введи целое число.',ephemeral=True)
        reason=str(self.reason).strip()
        status,balance,weekly=db.grant_points_unlimited(
            interaction.guild_id,self.member_id,interaction.user.id,amount,reason)
        if status!='ok':
            message='У игрока нет профиля MCL.' if status=='missing' else 'Введите число от 1 до 1 000 000.'
            return await interaction.response.send_message(message,ephemeral=True)
        record=db.player(interaction.guild_id,self.member_id)
        channel=interaction.guild.get_channel(record['personal_channel_id']) if record else None
        if isinstance(channel,discord.TextChannel):
            notification=discord.Embed(
                title='MCL | Админ-начисление',
                description=(f'Игрок: <@{self.member_id}>\n'
                             f'Начислено: **+{amount}**\n'
                             f'Причина: **{reason}**\n'
                             f'Новый баланс: **{balance}**\n'
                             f'Выдал: {interaction.user.mention}'),
                color=discord.Color.green())
            try:
                await channel.send(embed=notification)
            except discord.HTTPException:
                logging.exception('Could not send owner grant notification')
        await interaction.response.send_message(
            f'✅ {self.member_label}: **+{amount}**. Баланс: **{balance}**. '
            f'Недельный счётчик не изменён: **{weekly}/{db.LIMIT}**.',ephemeral=True)


class AdminUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder='Выбери зарегистрированного игрока…',min_values=1,max_values=1)

    async def callback(self,interaction:discord.Interaction):
        if not is_owner(interaction.user):
            return await interaction.response.send_message('Только Owner / администратор.',ephemeral=True)
        member=self.values[0]
        if member.bot:
            return await interaction.response.send_message('Боту нельзя начислять баллы.',ephemeral=True)
        if not db.player(interaction.guild_id,member.id):
            return await interaction.response.send_message('У этого пользователя нет профиля MCL.',ephemeral=True)
        await interaction.response.send_modal(UnlimitedGrantModal(member.id,member.display_name))


class AdminMemberSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(AdminUserSelect())


class BulkGrantConfirmView(discord.ui.View):
    def __init__(self,owner_id,amount,reason):
        super().__init__(timeout=120)
        self.owner_id=owner_id
        self.amount=amount
        self.reason=reason
        self.used=False

    async def interaction_check(self,interaction:discord.Interaction):
        if interaction.user.id!=self.owner_id or not is_owner(interaction.user):
            await interaction.response.send_message(
                'Подтвердить массовую выдачу может только Owner, который её создал.',ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Подтвердить выдачу всем',style=discord.ButtonStyle.danger,
                       emoji='✅')
    async def confirm(self,interaction:discord.Interaction,button:discord.ui.Button):
        if self.used:
            return await interaction.response.send_message('Эта выдача уже обработана.',ephemeral=True)
        self.used=True
        for item in self.children:
            item.disabled=True
        await interaction.response.defer(ephemeral=True,thinking=True)
        status,players=db.grant_points_all_unlimited(
            interaction.guild_id,interaction.user.id,self.amount,self.reason)
        if status!='ok':
            message=('В семье пока нет зарегистрированных профилей.' if status=='empty'
                     else 'Введите число от 1 до 1 000 000.')
            return await interaction.edit_original_response(content=message,view=None)
        notified=0
        notification=discord.Embed(
            title='MCL | Общее начисление',
            description=(f'Начислено: **+{self.amount} TP**\n'
                         f'Причина: **{self.reason}**\n'
                         f'Выдал: {interaction.user.mention}\n\n'
                         'Начисление не влияет на недельный лимит.'),
            color=discord.Color.green())
        for user_id,channel_id in players:
            channel=interaction.guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel,discord.TextChannel):
                continue
            try:
                await channel.send(content=f'<@{user_id}>',embed=notification)
                notified+=1
            except discord.HTTPException:
                logging.exception('Could not send bulk owner grant notification')
        count=len(players)
        await interaction.edit_original_response(
            content=(f'✅ Всем зарегистрированным игрокам (**{count}**) выдано '
                     f'по **{self.amount} TP**. Всего начислено: **{count*self.amount} TP**. '
                     f'Уведомления доставлены: **{notified}/{count}**.'),view=None)
        self.stop()

    @discord.ui.button(label='Отмена',style=discord.ButtonStyle.secondary,emoji='✖️')
    async def cancel(self,interaction:discord.Interaction,button:discord.ui.Button):
        self.used=True
        await interaction.response.edit_message(content='Массовая выдача отменена.',view=None)
        self.stop()


class BulkGrantModal(discord.ui.Modal,title='MCL | Выдать баллы всей семье'):
    amount=discord.ui.TextInput(label='Баллов каждому',placeholder='Например: 20',
                                min_length=1,max_length=7)
    reason=discord.ui.TextInput(label='Причина',style=discord.TextStyle.paragraph,
                                placeholder='Еженедельная награда всей семье',
                                min_length=3,max_length=250)

    async def on_submit(self,interaction:discord.Interaction):
        if not is_owner(interaction.user):
            return await interaction.response.send_message('Только Owner / администратор.',ephemeral=True)
        try:
            amount=int(str(self.amount).strip())
        except ValueError:
            return await interaction.response.send_message('Введи целое число.',ephemeral=True)
        if amount<=0 or amount>1000000:
            return await interaction.response.send_message(
                'Введите число от 1 до 1 000 000.',ephemeral=True)
        reason=str(self.reason).strip()
        count=db.bulk_grant_target_count(interaction.guild_id)
        if count==0:
            return await interaction.response.send_message(
                'В семье пока нет зарегистрированных профилей.',ephemeral=True)
        await interaction.response.send_message(
            f'⚠️ Выдать **{count}** игрокам по **{amount} TP**? '
            f'Всего будет начислено **{count*amount} TP**.\nПричина: **{reason}**',
            view=BulkGrantConfirmView(interaction.user.id,amount,reason),ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Регистрация',style=discord.ButtonStyle.primary,custom_id='mcl:register')
    async def register(self,interaction:discord.Interaction,button:discord.ui.Button):
        await interaction.response.send_modal(RegisterModal())

    @discord.ui.button(label='Мой профиль',style=discord.ButtonStyle.secondary,custom_id='mcl:profile')
    async def profile(self,interaction:discord.Interaction,button:discord.ui.Button):
        record = db.player(interaction.guild_id,interaction.user.id)
        if record is None:
            return await interaction.response.send_message('Сначала зарегистрируйся.',ephemeral=True)
        em=profile_embed(interaction.user,record)
        cid=record['personal_channel_id']
        if cid:
            ch=interaction.guild.get_channel(cid)
            if ch:
                em.add_field(name='Мой архив',value=ch.mention,inline=False)
        await interaction.response.send_message(embed=em,ephemeral=True)

    @discord.ui.button(label='Подать на баллы',style=discord.ButtonStyle.success,custom_id='mcl:evidence')
    async def submit(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not db.player(interaction.guild_id,interaction.user.id):
            return await interaction.response.send_message('Сначала зарегистрируйся.',ephemeral=True)
        await interaction.response.send_modal(ActivityModal())

    @discord.ui.button(label='Магазин',style=discord.ButtonStyle.primary,custom_id='mcl:shop:panel')
    async def shop(self,interaction:discord.Interaction,button:discord.ui.Button):
        await open_shop(interaction)

    @discord.ui.button(label='Админ: выдать баллы',style=discord.ButtonStyle.danger,
                       custom_id='mcl:admin:grant',row=1)
    async def admin_grant(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not is_owner(interaction.user):
            return await interaction.response.send_message('Только Owner / администратор.',ephemeral=True)
        await interaction.response.send_message(
            'Выбери игрока. Начисление увеличит баланс без недельного лимита.',
            view=AdminMemberSelectView(),ephemeral=True)

    @discord.ui.button(label='Админ: выдать всем',style=discord.ButtonStyle.danger,
                       custom_id='mcl:admin:grant_all',row=1)
    async def admin_grant_all(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not is_owner(interaction.user):
            return await interaction.response.send_message('Только Owner / администратор.',ephemeral=True)
        await interaction.response.send_modal(BulkGrantModal())


async def open_shop(interaction):
    if not db.player(interaction.guild_id,interaction.user.id):
        return await interaction.response.send_message('Сначала зарегистрируйся.',ephemeral=True)
    items = db.shop_items(interaction.guild_id)
    if not items:
        return await interaction.response.send_message('Магазин пока не заполнен.',ephemeral=True)
    await interaction.response.send_message(
        embed=shop_embed(interaction.guild_id),view=ShopSelectView(items),ephemeral=True)


class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Открыть магазин',style=discord.ButtonStyle.primary,
                       emoji='🛒',custom_id='mcl:shop:open')
    async def open(self,interaction:discord.Interaction,button:discord.ui.Button):
        await open_shop(interaction)


class ShopSelect(discord.ui.Select):
    def __init__(self,items):
        options=[]
        for item in items:
            description = f'{item["price"]} баллов'
            if item['description']:
                description += f' • {item["description"]}'
            options.append(discord.SelectOption(
                label=item['name'][:100],value=str(item['id']),
                description=description[:100],emoji='🎁'))
        super().__init__(placeholder='Выбери товар…',min_values=1,max_values=1,
                         options=options,custom_id='mcl:shop:select')

    async def callback(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        item_id=int(self.values[0])
        item=db.shop_item(interaction.guild_id,item_id)
        if item is None:
            return await interaction.followup.send('Этот товар больше недоступен.',ephemeral=True)
        try:
            channel=await ensure_personal_channel(interaction.guild,interaction.user)
        except (discord.HTTPException,ValueError) as exc:
            return await interaction.followup.send(str(exc),ephemeral=True)
        status,request_id,balance=db.create_purchase_request(
            interaction.guild_id,interaction.user.id,item_id)
        if status!='ok':
            reasons={
                'missing':'Этот товар больше недоступен.',
                'unregistered':'Сначала зарегистрируйся.',
                'duplicate':f'У тебя уже есть заявка на этот товар (#{request_id}).',
                'insufficient':f'Недостаточно баллов. Баланс: **{balance}**, цена: **{item["price"]}**.',
            }
            return await interaction.followup.send(reasons[status],ephemeral=True)
        em=discord.Embed(
            title=f'MCL | Покупка #{request_id}',
            description=(f'Покупатель: {interaction.user.mention}\n'
                         f'Товар: **{item["name"]}**\n'
                         f'Цена: **{item["price"]} баллов**\n'
                         f'Баланс до покупки: **{balance}**\n'
                         'Статус: **Ожидает подтверждения**'),
            color=discord.Color.orange())
        try:
            review=await channel.send(embed=em,view=PurchaseReviewView(request_id))
        except discord.HTTPException:
            db.reject_purchase(request_id,0,'Не удалось отправить запрос в личный канал.')
            logging.exception('Could not create purchase review message')
            return await interaction.followup.send('Не удалось отправить запрос. Попробуй позже.',ephemeral=True)
        db.attach_purchase_request(request_id,channel.id,review.id)
        await interaction.followup.send(
            f'📨 Запрос на **{item["name"]}** отправлен в {channel.mention}. '
            'Баллы спишутся после одобрения Owner / Tier Check.',ephemeral=True)


class ShopSelectView(discord.ui.View):
    def __init__(self,items):
        super().__init__(timeout=300)
        self.add_item(ShopSelect(items))


async def finish_review(interaction,submission_id,status,details):
    s = db.submission(submission_id)
    if s is None:
        return
    em = discord.Embed(title=f'MCL | Откат #{submission_id}',
        description=f'<@{s["user_id"]}>\nМероприятие: {s["activity"] or "Откат"}\n**{status}**\n{details}',
        color=discord.Color.green() if status=='Одобрено' else discord.Color.red())
    try:
        if isinstance(interaction.channel,discord.TextChannel):
            review_message = await interaction.channel.fetch_message(s["message_id"])
            await review_message.edit(embed=em,view=None)
    except discord.HTTPException:
        logging.exception('Could not update review message')


class ApproveModal(discord.ui.Modal,title='MCL | Начислить баллы'):
    amount = discord.ui.TextInput(label='Количество баллов (1–160)',placeholder='Например: 25',max_length=3)

    def __init__(self,submission_id):
        super().__init__()
        self.submission_id = submission_id

    async def on_submit(self,interaction:discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('Нет доступа.',ephemeral=True)
        try:
            requested = int(str(self.amount).strip())
        except ValueError:
            return await interaction.response.send_message('Введи целое число.',ephemeral=True)
        status,granted,weekly = db.approve(self.submission_id,interaction.user.id,requested)
        if status!='ok':
            reasons = {'closed':'Заявка уже обработана.','limit':'Игрок уже достиг 160 баллов за неделю.','invalid':'Введите число от 1 до 160.'}
            return await interaction.response.send_message(reasons[status],ephemeral=True)
        await interaction.response.send_message(f'✅ Начислено **{granted}** баллов. За неделю: **{weekly}/160**.'+
            (f' Запрошено {requested}, но сработал лимит.' if requested!=granted else ''),ephemeral=True)
        await finish_review(interaction,self.submission_id,'Одобрено',f'Начислено: **+{granted}** | Неделя: **{weekly}/160**')


class RejectModal(discord.ui.Modal,title='MCL | Отклонить откат'):
    reason = discord.ui.TextInput(label='Причина отказа',style=discord.TextStyle.paragraph,
                                  max_length=250,min_length=3)

    def __init__(self,submission_id):
        super().__init__()
        self.submission_id = submission_id

    async def on_submit(self,interaction:discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('Нет доступа.',ephemeral=True)
        result = db.reject(self.submission_id,interaction.user.id,str(self.reason))
        if not result:
            return await interaction.response.send_message('Заявка уже обработана.',ephemeral=True)
        await interaction.response.send_message('❌ Заявка отклонена.',ephemeral=True)
        await finish_review(interaction,self.submission_id,'Отклонено',str(self.reason))


class ReviewView(discord.ui.View):
    def __init__(self,submission_id):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.add_item(ApproveButton(submission_id))
        self.add_item(RejectButton(submission_id))


class ApproveButton(discord.ui.Button):
    def __init__(self,sid):
        super().__init__(label='Одобрить',style=discord.ButtonStyle.success,custom_id=f'mcl:approve:{sid}')
        self.sid=sid

    async def callback(self,interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('Только Owner / Tier Check.',ephemeral=True)
        await interaction.response.send_modal(ApproveModal(self.sid))


class RejectButton(discord.ui.Button):
    def __init__(self,sid):
        super().__init__(label='Отклонить',style=discord.ButtonStyle.danger,custom_id=f'mcl:reject:{sid}')
        self.sid=sid

    async def callback(self,interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('Только Owner / Tier Check.',ephemeral=True)
        await interaction.response.send_modal(RejectModal(self.sid))


async def finish_purchase_review(interaction,request_id,status,details):
    request=db.purchase_request(request_id)
    if request is None:
        return
    em=discord.Embed(
        title=f'MCL | Покупка #{request_id}',
        description=(f'<@{request["user_id"]}>\n'
                     f'Товар: **{request["item_name"]}**\n'
                     f'Цена: **{request["price"]} баллов**\n'
                     f'**{status}**\n{details}'),
        color=discord.Color.green() if status=='Одобрено' else discord.Color.red())
    try:
        channel=interaction.guild.get_channel(request['channel_id'])
        if isinstance(channel,discord.TextChannel):
            message=await channel.fetch_message(request['message_id'])
            await message.edit(embed=em,view=None)
    except discord.HTTPException:
        logging.exception('Could not update purchase review message')


class PurchaseApproveButton(discord.ui.Button):
    def __init__(self,request_id):
        super().__init__(label='Одобрить покупку',style=discord.ButtonStyle.success,
                         custom_id=f'mcl:purchase:approve:{request_id}')
        self.request_id=request_id

    async def callback(self,interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                'Только Owner / Tier Check.',ephemeral=True)
        status,balance=db.approve_purchase(self.request_id,interaction.user.id)
        if status!='ok':
            reasons={
                'closed':'Запрос уже обработан.',
                'insufficient':f'У игрока уже недостаточно баллов. Текущий баланс: **{balance}**.',
            }
            return await interaction.response.send_message(reasons[status],ephemeral=True)
        await interaction.response.send_message(
            f'✅ Покупка одобрена. Новый баланс игрока: **{balance}**.',ephemeral=True)
        await finish_purchase_review(
            interaction,self.request_id,'Одобрено',f'Баллы списаны. Новый баланс: **{balance}**')


class PurchaseRejectModal(discord.ui.Modal,title='MCL | Отклонить покупку'):
    reason=discord.ui.TextInput(label='Причина отказа',style=discord.TextStyle.paragraph,
                                min_length=3,max_length=250)

    def __init__(self,request_id):
        super().__init__()
        self.request_id=request_id

    async def on_submit(self,interaction:discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message('Нет доступа.',ephemeral=True)
        result=db.reject_purchase(self.request_id,interaction.user.id,str(self.reason))
        if not result:
            return await interaction.response.send_message('Запрос уже обработан.',ephemeral=True)
        await interaction.response.send_message('❌ Покупка отклонена.',ephemeral=True)
        await finish_purchase_review(
            interaction,self.request_id,'Отклонено',str(self.reason))


class PurchaseRejectButton(discord.ui.Button):
    def __init__(self,request_id):
        super().__init__(label='Отклонить',style=discord.ButtonStyle.danger,
                         custom_id=f'mcl:purchase:reject:{request_id}')
        self.request_id=request_id

    async def callback(self,interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                'Только Owner / Tier Check.',ephemeral=True)
        await interaction.response.send_modal(PurchaseRejectModal(self.request_id))


class PurchaseReviewView(discord.ui.View):
    def __init__(self,request_id):
        super().__init__(timeout=None)
        self.add_item(PurchaseApproveButton(request_id))
        self.add_item(PurchaseRejectButton(request_id))


class MCLBot(commands.Bot):
    def __init__(self):
        intents=discord.Intents.default()
        intents.message_content=True
        super().__init__(command_prefix='!',intents=intents)

    async def setup_hook(self):
        logging.info("SQLite database path: %s", db.DB_PATH)
        db.init_db()
        db.init_v2()
        db.init_v3()
        self.add_view(PanelView())
        self.add_view(ShopPanelView())
        for s in db.pending_submissions():
            self.add_view(ReviewView(s['id']),message_id=s['message_id'])
        for request in db.pending_purchase_requests():
            self.add_view(PurchaseReviewView(request['id']),message_id=request['message_id'])
        if GUILD_ID:
            guild=discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot=MCLBot()


@bot.tree.command(name='setup',description='Опубликовать панель управления MCL')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setup(interaction:discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Команда только для администраторов.',ephemeral=True)
    em=discord.Embed(title='🏆 MCL | FAMILY POINTS',
        description=('**Регистрация:** создай игровой профиль.\n'
                     '**Мой профиль:** проверь баланс и баллы.\n'
                     '**Подать на баллы:** приложи скриншот/видео в личном канале.\n'
                     '**Магазин:** выбери награду и отправь запрос на покупку.\n\n'
                     '**Лимит: 160 баллов за неделю.**'),
        color=discord.Color.gold())
    await interaction.channel.send(embed=em,view=PanelView())
    await interaction.response.send_message('✅ Панель опубликована.',ephemeral=True)


@bot.tree.command(name='profile',description='Посмотреть профиль MCL')
@app_commands.guild_only()
async def profile(interaction:discord.Interaction):
    p=db.player(interaction.guild_id,interaction.user.id)
    if not p:
        return await interaction.response.send_message('Нажми «Регистрация» в панели MCL.',ephemeral=True)
    await interaction.response.send_message(embed=profile_embed(interaction.user,p),ephemeral=True)


@bot.tree.command(name='shop',description='Открыть магазин MCL')
@app_commands.guild_only()
async def shop(interaction:discord.Interaction):
    await open_shop(interaction)


@bot.tree.command(name='shop_setup',description='Опубликовать сообщение магазина MCL')
@app_commands.guild_only()
async def shop_setup(interaction:discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / Tier Check.',ephemeral=True)
    if not db.shop_items(interaction.guild_id):
        return await interaction.response.send_message(
            'Сначала добавь товары командой `/shop_item_set`.',ephemeral=True)
    await interaction.channel.send(embed=shop_panel_embed(),view=ShopPanelView())
    await interaction.response.send_message('✅ Магазин опубликован.',ephemeral=True)


@bot.tree.command(name='shop_item_set',description='Добавить или обновить товар магазина')
@app_commands.guild_only()
@app_commands.describe(name='Название товара',price='Цена в баллах',
                       description='Краткое описание награды')
async def shop_item_set(interaction:discord.Interaction,name:str,price:int,
                        description:str=''):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / Tier Check.',ephemeral=True)
    name=name.strip()
    description=description.strip()
    if not 2 <= len(name) <= 100:
        return await interaction.response.send_message(
            'Название должно содержать 2–100 символов.',ephemeral=True)
    if not 1 <= price <= 100000:
        return await interaction.response.send_message(
            'Цена должна быть от 1 до 100000 баллов.',ephemeral=True)
    if len(description) > 200:
        return await interaction.response.send_message(
            'Описание не должно быть длиннее 200 символов.',ephemeral=True)
    item=db.set_shop_item(interaction.guild_id,name,price,description)
    await interaction.response.send_message(
        f'✅ Товар **{item["name"]}** сохранён. Цена: **{item["price"]}**.',ephemeral=True)


@bot.tree.command(name='shop_item_remove',description='Убрать товар из магазина')
@app_commands.guild_only()
@app_commands.describe(name='Точное название товара')
async def shop_item_remove(interaction:discord.Interaction,name:str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / Tier Check.',ephemeral=True)
    if not db.deactivate_shop_item(interaction.guild_id,name.strip()):
        return await interaction.response.send_message('Активный товар не найден.',ephemeral=True)
    await interaction.response.send_message(f'✅ Товар **{name.strip()}** убран.',ephemeral=True)


@bot.tree.command(name='shop_items',description='Показать текущие товары магазина')
@app_commands.guild_only()
async def shop_items(interaction:discord.Interaction):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / Tier Check.',ephemeral=True)
    await interaction.response.send_message(embed=shop_embed(interaction.guild_id),ephemeral=True)


@bot.tree.command(name='points_adjust',description='Добавить или снять баллы у игрока')
@app_commands.guild_only()
@app_commands.choices(action=[
    app_commands.Choice(name='Добавить',value='add'),
    app_commands.Choice(name='Снять',value='subtract'),
])
@app_commands.describe(member='Игрок',action='Добавить или снять',
                       amount='Количество баллов',reason='Причина изменения')
async def points_adjust(interaction:discord.Interaction,member:discord.Member,
                        action:app_commands.Choice[str],amount:int,reason:str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / Tier Check.',ephemeral=True)
    if not 1 <= amount <= 100000:
        return await interaction.response.send_message(
            'Количество должно быть от 1 до 100000.',ephemeral=True)
    reason=reason.strip()
    if not 3 <= len(reason) <= 250:
        return await interaction.response.send_message(
            'Причина должна содержать 3–250 символов.',ephemeral=True)
    signed_amount=amount if action.value=='add' else -amount
    status,actual,balance,weekly=db.adjust_points(
        interaction.guild_id,member.id,interaction.user.id,signed_amount,reason)
    if status!='ok':
        messages={
            'missing':'У игрока нет профиля MCL.',
            'limit':'Игрок уже достиг недельного лимита 160.',
            'insufficient':f'Нельзя снять больше баланса. Текущий баланс: **{balance}**.',
            'invalid':'Некорректное количество баллов.',
        }
        return await interaction.response.send_message(messages[status],ephemeral=True)
    sign='+' if actual>0 else ''
    limited=(actual != signed_amount)
    record=db.player(interaction.guild_id,member.id)
    channel=interaction.guild.get_channel(record['personal_channel_id']) if record else None
    if isinstance(channel,discord.TextChannel):
        notification=discord.Embed(
            title='MCL | Изменение баланса',
            description=(f'Игрок: {member.mention}\n'
                         f'Изменение: **{sign}{actual}**\n'
                         f'Причина: **{reason}**\n'
                         f'Новый баланс: **{balance}**\n'
                         f'Изменил: {interaction.user.mention}'),
            color=discord.Color.green() if actual>0 else discord.Color.red())
        try:
            await channel.send(embed=notification)
        except discord.HTTPException:
            logging.exception('Could not send balance adjustment notification')
    await interaction.response.send_message(
        f'✅ {member.mention}: **{sign}{actual}** баллов. '
        f'Баланс: **{balance}**, за неделю: **{weekly}/{db.LIMIT}**.'+
        (' Сумма уменьшена из-за недельного лимита.' if limited else ''),ephemeral=True)


@bot.tree.command(name='admin_grant',description='Owner: выдать баллы без недельного лимита')
@app_commands.guild_only()
async def admin_grant(interaction:discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / администратор.',ephemeral=True)
    await interaction.response.send_message(
        'Выбери игрока. Начисление увеличит баланс и не изменит недельный счётчик.',
        view=AdminMemberSelectView(),ephemeral=True)


@bot.tree.command(name='admin_grant_all',description='Owner: выдать баллы всей семье вне лимита')
@app_commands.guild_only()
async def admin_grant_all(interaction:discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message(
            'Только Owner / администратор.',ephemeral=True)
    await interaction.response.send_modal(BulkGrantModal())


@bot.tree.command(name='leaderboard',description='Топ игроков MCL по баллам за неделю')
@app_commands.guild_only()
async def leaderboard(interaction:discord.Interaction):
    rows=db.weekly_leaderboard(interaction.guild_id)
    lines=[f'**{i}.** {r["nickname"]}: **{r["score"]}**' for i,r in enumerate(rows,1)]
    em=discord.Embed(title='🏆 MCL | Топ недели',description='\n'.join(lines) if lines else 'Пока нет игроков.',color=discord.Color.gold())
    await interaction.response.send_message(embed=em)


@bot.event
async def on_message(message:discord.Message):
    if message.author.bot or not message.guild or not isinstance(message.channel,discord.TextChannel):
        return
    record=db.player_for_channel(message.guild.id,message.channel.id)
    if not record or record['user_id']!=message.author.id:
        return
    intent=db.get_upload(message.guild.id,message.author.id)
    if not intent:
        return  # Ordinary archive messages are not point applications.
    valid=[]
    for att in message.attachments:
        typ=(att.content_type or '').lower()
        if typ.startswith(('image/','video/')) or att.filename.lower().endswith(
            ('.png','.jpg','.jpeg','.webp','.gif','.mp4','.mov','.webm','.mkv')):
            valid.append(att.url)
    for token in message.content.split():
        if token.startswith(('https://','http://')):
            valid.append(token)
    if not valid:
        await message.reply('Прикрепи изображение, видео или ссылку. Заявка пока не создана.',mention_author=False)
        return
    evidence='\n'.join(valid[:5])
    sid=db.create_activity_submission(message.guild.id,message.author.id,evidence,str(intent['activity']))
    db.consume_upload(message.guild.id,message.author.id)
    em=discord.Embed(title=f'MCL | Заявка на баллы #{sid}',
        description=f'Игрок: {message.author.mention}\nМероприятие: **{intent["activity"]}**\n'
                    f'Доказательство: [открыть сообщение]({message.jump_url})\n'
                    'Статус: **Ожидает проверки**',color=discord.Color.orange())
    review=await message.channel.send(embed=em,view=ReviewView(sid))
    db.attach_submission(sid,message.channel.id,review.id)
    await message.add_reaction('📨')


@bot.event
async def on_ready():
    logging.info('MCL bot online: %s',bot.user)

if __name__=='__main__':
    if not TOKEN:
        raise SystemExit('Заполни DISCORD_TOKEN в .env')
    bot.run(TOKEN)
