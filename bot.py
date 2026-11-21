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


def profile_embed(member, record):
    total, weekly = db.points(member.guild.id, member.id)
    em = discord.Embed(title='MCL | Профиль игрока', color=discord.Color.gold())
    em.add_field(name='Игровой ник',value=record['nickname'],inline=False)
    em.add_field(name='За неделю',value=f'{weekly} / {db.LIMIT}',inline=True)
    em.add_field(name='За всё время',value=str(total),inline=True)
    em.add_field(name='Неделя с',value=db.week_key(),inline=False)
    em.set_footer(text='Недельный счёт обновляется автоматически по понедельникам (Europe/Zagreb)')
    return em


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


class MCLBot(commands.Bot):
    def __init__(self):
        intents=discord.Intents.default()
        intents.message_content=True
        super().__init__(command_prefix='!',intents=intents)

    async def setup_hook(self):
        logging.info("SQLite database path: %s", db.DB_PATH)
        db.init_db()
        db.init_v2()
        self.add_view(PanelView())
        for s in db.pending_submissions():
            self.add_view(ReviewView(s['id']),message_id=s['message_id'])
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
        description='**Регистрация:** создай игровой профиль.\n**Мой профиль:** проверь баллы.\n**Подать на баллы:** приложи скриншот/видео в личном канале.\n\n**Лимит: 160 баллов за неделю.**',
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


@bot.tree.command(name='leaderboard',description='Топ игроков MCL по баллам за неделю')
@app_commands.guild_only()
async def leaderboard(interaction:discord.Interaction):
    with db.connect() as conn:
        rows=conn.execute('''SELECT p.user_id,p.nickname,COALESCE(SUM(a.points),0) AS score
            FROM players p LEFT JOIN awards a ON a.guild_id=p.guild_id AND a.user_id=p.user_id AND a.week_key=?
            WHERE p.guild_id=? GROUP BY p.user_id,p.nickname ORDER BY score DESC,p.nickname ASC LIMIT 10''',
            (db.week_key(),interaction.guild_id)).fetchall()
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
