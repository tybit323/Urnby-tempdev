# Builtin
import datetime
import json
import asyncio
import copy
from enum import Enum
from pathlib import Path

# External
import discord
from discord.ext import commands
from aiosqlite import OperationalError
from pycord.multicog import add_to_group

# Internal
import data.databaseapi as db
import static.common as com
from views.SkipQueueView import SkipQueueView
from views.ClearOutView import ClearOutView
from checks.IsAdmin import is_admin, NotAdmin
from checks.IsCommandChannel import is_command_channel, NotCommandChannel
from checks.IsMemberVisible import is_member_visible, NotMemberVisible
from checks.IsMember import is_member, NotMember
from checks.IsInDev import is_in_dev, InDevelopment

# Since time is important for this application the strategy is as follows:
# Create datetime 
# Store as timestamp integer (this strips TZ info and stored value is not timezone specific)
# Any Values displayed will utilize discord <t:[timestamp]:f> for accessing
#   Unless monospaced format is wanted (ie: ``` ```) for formating purposes, will display as EST timezone (need to assign tz info)
# Any stored values NOT as integer timestamps are for info/debug ONLY do not access isoformats

class MemberQueryResult(Enum):
    FOUND = 1
    ID_NOT_FOUND = 2
    NOT_UNIQUE = 3
    UNKNOWN_PARAMETER = 4
    QUERY_FAILED = 5
    

class Clocks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state_lock = asyncio.Lock()
        
        print('Initilization on clocks complete', flush=True)
        
    admin_group = discord.commands.SlashCommandGroup('admin')
    get_group = discord.commands.SlashCommandGroup('get')
    session_group = discord.commands.SlashCommandGroup('session')
    
    @commands.Cog.listener()
    async def on_ready(self):
        missing_tables = await db.check_tables(['historical', 'session', 'session_history', 'active', 'commands'])
        if missing_tables:
            print(f"Warning, missing the following tables in db: {missing_tables}")
        #saving for example to get handle on other cogs
        #self.cq = self.bot.get_cog('CampQueue')
    
    async def cog_before_invoke(self, ctx):
        guild_id = 0
        if ctx.guild:
            guild_id = ctx.guild.id
        now_iso = com.get_current_iso()
        print(f'{now_iso} [{guild_id}] - Command {ctx.command.qualified_name} by {ctx.author.name} - {ctx.author.id} - {ctx.selected_options}', flush=True)
        command = {'command_name': ctx.command.qualified_name, 'options': str(ctx.selected_options), 'datetime': now_iso, 'user': ctx.author.id, 'user_name': ctx.author.name, 'channel_name': ctx.channel.name}
        await db.store_command(guild_id, command)
        return
    
    # ==============================================================================
    # Error Handlers
    # ==============================================================================
    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        now = com.get_current_datetime()
        guild_id = None
        channel_name = None
        if not ctx.guild:
            guild_id = 'DM'
            channel_name = 'DM'
        else:
            guild_id = ctx.guild.id
            channel_name = ctx.channel.name
        print(f'{now.isoformat()} [{guild_id}] - Error in command {ctx.command.qualified_name} by {ctx.author.name} - {ctx.author.id} {error}', flush=True)
        _error = {
            'level': 'error', 
            'command_name': ctx.command.qualified_name, 
            'options': str(ctx.selected_options), 
            'author_id': ctx.author.id, 
            'author_name': ctx.author.name, 
            'channel_name': channel_name, 
            'error': str(type(error)),
        }
        
        if isinstance(error, NotAdmin):
            await ctx.send_response(content=f"You do not have permissions to use this function, {ctx.command} - {ctx.selected_options}")
            return
        elif isinstance(error, NotCommandChannel):
            await ctx.send_response(content=f"You can not perform this command in this channel", ephemeral=True)
            return
        elif isinstance(error, NotMemberVisible):
            await ctx.send_response(content=f"This command can not be performed where other members can not see the command", ephemeral=True)
            return
        elif isinstance(error, NotMember):
            await ctx.send_response(content=f"You must be a member of higher privileges to invoke this command", ephemeral=False)
            return
        elif isinstance(error, InDevelopment):
            await ctx.send_response(content=f"This function is unavailable due to it's development status", ephemeral=True)
            return
        else:
            print(type(error), flush=True)
            raise error
        return
    
    # ==============================================================================
    # Init/Reconnection events
    # ==============================================================================
    @commands.Cog.listener()
    async def on_guild_join(guild):
        print(f'Joined {guild} guild', flush=True)
    
    @commands.Cog.listener()
    async def on_connect(self):
        print(f'clocks connected to discord',flush=True)
        await db.set_db_to_wal()
        
    # ==============================================================================
    # All User Commands
    # ============================================================================== 

    @get_group.command(name='config', description='Ephemeral optional - Get bot configuration')
    async def _get_config(self, ctx, public: discord.Option(bool, name='public', default=False)):
        if ctx.guild is None:
            await ctx.send_response(content='This command can not be used in Direct Messages')
            return
        config = self.get_config(ctx.guild.id)
        await ctx.send_response(content=f"{config}", ephemeral=not public)
    
    # ==============================================================================
    # Activity Commands
    # ==============================================================================
    @get_group.command(name='active', description='Ephemeral optional - Get list of active users in the session')
    @is_member()
    async def _get_active(self, ctx, public: discord.Option(bool, name='public', default=False)):
        actives = await db.get_all_actives(ctx.guild.id)
        timestamp_now = com.get_current_timestamp()
        if len(actives) == 0:
            await ctx.send_response(content=f"There are no active users at this time", ephemeral=not public)
            return
        content = "_ _\nActive Users:\n```"
        for active in actives:
            user = await ctx.guild.fetch_member(active['user'])
            delta = com.get_hours_from_secs(timestamp_now - active['in_timestamp'])
            content += f"\n{user.display_name[:19]:20}{delta:.2f} hours active"
        content += "```"
        await ctx.send_response(content=content, ephemeral=not public)
    
    @commands.slash_command(name='clockin', description='Clock into the active session')
    @is_member()
    @is_member_visible()
    @is_command_channel()
    async def _clockin(self, ctx, character: discord.Option(str, name='character', required=False, default='')):
        # Session Check
        session = await db.get_session(ctx.guild.id)
        if not session:
            await ctx.send_response(content=f'Sorry, there is no current session to clock into')
            return
        # Already active check
        actives = await db.get_all_actives(ctx.guild.id)
        
        for active in actives:
            if active['user'] == ctx.author.id:
                await ctx.send_response(content=f'You are already active, did you mean to clockout?')
                return
        
        now = com.get_current_datetime()
        # Create entry and store
        doc = {
                'user': ctx.author.id,
                'character': character,
                'session': session['session'],
                'in_timestamp': int(now.timestamp()),
                'out_timestamp': '',
                '_DEBUG_user_name': ctx.author.display_name,
                '_DEBUG_in': now.isoformat(),
                '_DEBUG_out': '',
                '_DEBUG_delta': '',
            }
        content = ''
        older_reps = await db.get_replacements_before_user(ctx.guild.id, ctx.author.id)
        if older_reps:
            view = SkipQueueView()
            await ctx.send_response("There are members ahead of you in the rep queue, are you sure you want to remove them from the queue and skip to clockin?", view=view)
            await view.wait()
            if view.result == False:
                # Time out
                return
            elif view.result == True:
                '''
                content = f'Removing these replacements which are OLDER than this replacement:'
                for rep in older_reps:
                    await remove_rep(ctx, rep['user'])
                    content += f'\n<@{rep["user"]}> @ {com.datetime_from_timestamp(rep["in_timestamp"]).isoformat()}'
                
                content += '\n'
                '''
                content += f'Skipping {len(older_reps)} replacements and clocking {ctx.author.display_name} in. Alerting queuers to adjust their status: '
                for rep in older_reps:
                    content += f'<@{rep["user"]}> '
                content += '\n'
                
        rep_removed = await db.remove_replacement(ctx.guild.id, ctx.author.id)
        
        content += f'{ctx.author.display_name} {com.scram("Successfully")} clocked in at <t:{doc["in_timestamp"]}:f>'
        if rep_removed is not None:
            content += f' and was removed from replacement list'
        
        await db.store_active_record(ctx.guild.id, doc)
        try:
            await ctx.send_response(content=content)
        except (discord.errors.InteractionResponded, RuntimeError):
            await ctx.send_followup(content=content)
        
        config = self.get_config(ctx.guild.id)
        if 'max_active' in config.keys() and config['max_active'] < len(actives)+1:
            actives = await db.get_all_actives(ctx.guild.id)
            content = f'Max number of active users is {config["max_active"]}, we are at {len(actives)} currently'
            for active in actives:
                content += f', <@{active["user"]}>'
            content = content + " please reduce active users"
            await ctx.send_followup(content=content)
            return
        return
    
    @commands.slash_command(name='clockout', description='Clock out of the active session')
    @is_member()
    @is_member_visible()
    @is_command_channel()
    async def _clockout(self, ctx, userid: discord.Option(str, name='userid', required=False, default=None)):
        target = ctx.author.id
        if userid is not None:
            target = await check_user_id(ctx, userid)
            if target is None:
                return
        
        res = await self._inner_clockout(ctx, target)
        
        await ctx.send_response(content=res['content'])
        if res['status'] == False:
            return
        
        bonus_sessions = await self.get_bonus_sessions(ctx.guild.id, res['record'], res['row'])
        member = await ctx.guild.fetch_member(target)
        for item in bonus_sessions:
            row = await db.store_new_historical(ctx.guild.id, item)
            tot = await db.get_user_hours(ctx.guild.id, member.id)
            
            await ctx.send_followup(content=f'{member.display_name} Obtained bonus hours, stored record #{row} for {item["_DEBUG_delta"]} hours. Your total is at {round(tot, 2)}')
    
    @commands.user_command(name="Clockout User")
    @is_member()
    async def _user_clockout(self, ctx, member: discord.Member):
        res = await self._inner_clockout(ctx, member.id)
        await ctx.send_response(content=f"{ctx.author.display_name} attempted to clock out user {member.display_name}. {res['content']}\n")
        if res['status'] == False:
            return
        bonus_sessions = await self.get_bonus_sessions(ctx.guild.id, res['record'], res['row'])
        for item in bonus_sessions:
            row = await db.store_new_historical(ctx.guild.id, item)
            tot = await db.get_user_hours(ctx.guild.id, member.id)
            await ctx.send_followup(content=f'{member.display_name} Obtained bonus hours, stored record #{row} for {item["_DEBUG_delta"]} hours. User total is at {round(tot, 2)}')
        return
    
    async def get_bonus_sessions(self, guild_id, record, row):
        config = self.get_config(guild_id)
        if not config.get('bonus_hours'):
            return None
        bonuses = []
        
        for bonus in config['bonus_hours']:
            _in = com.datetime_from_timestamp(record['in_timestamp'])
            _out = com.datetime_from_timestamp(record['out_timestamp'])
            for day in range((_out.date() - _in.date()).days+1):
                bonus_in = com.datetime_combine((_in.date()+datetime.timedelta(days=day)).isoformat(), bonus['start'])
                bonus_out = com.datetime_combine((_in.date()+datetime.timedelta(days=day)).isoformat(), bonus['end'])
                if _in <= bonus_out and _out >= bonus_in:
                    
                    print(f'{com.get_current_iso()} [{guild_id}] - Bonus hours found for {record["_DEBUG_user_name"]}', flush=True)
                    #we have an intersection
                    #duration calculation
                    duration = int(min(_out.timestamp()-_in.timestamp(), 
                                   _out.timestamp()-bonus_in.timestamp(), 
                                   bonus_out.timestamp()-_in.timestamp(), 
                                   bonus_out.timestamp()-bonus_in.timestamp()))
                    duration = int(duration * (float(bonus['pct'])/100))
                    start = _in if _in > bonus_in else bonus_in
                    rec = copy.deepcopy(record)
                    rec['character'] = f'{bonus["pct"]}_PCT_BONUS_{bonus["start"]}_TO_{bonus["end"]} {row}'
                    rec['in_timestamp'] = int(start.timestamp())
                    rec['out_timestamp'] = int(start.timestamp()+duration)
                    rec['_DEBUG_in'] = start.isoformat()
                    rec['_DEBUG_out'] = (start + datetime.timedelta(seconds=duration)).isoformat()
                    rec['_DEBUG_delta'] = com.get_hours_from_secs(duration)
                    bonuses.append(rec)
        return bonuses

    async def _inner_clockout(self, ctx, user_id):
        # Session Check
        session = await db.get_session(ctx.guild.id)
        if not session:
            return {'status': False, 'record': None, 'row': None, 'content': f'Sorry, there is no current session to clock out of'}
        
        # Ensure user was unique in active
        actives = await db.get_all_actives(ctx.guild.id)
        found = [_ for _ in actives if _['user'] == user_id]
        if not found:
            return {'status': False, 'record': None, 'row': None, 'content': f'Did not find you in active records, did you forget to clock in?'}
        if len(found) > 1:
            #error somehow they are clocked in more then once
            raise ValueError(f'Error - user was clocked in more then once guild: {ctx.guild.id} - user: {user_id}')
            return {'status': False, 'record': found, 'row': None, 'content': f'Error - user was clocked in more then once guild: {ctx.guild.id} - user: {user_id}'}
        record = found[0]
        
        res = await db.remove_active_record(ctx.guild.id, record)
        
        _out = com.get_current_datetime()
        record['_DEBUG_out'] = _out.isoformat()
        record['out_timestamp'] = int(_out.timestamp())
        record['_DEBUG_delta'] = com.get_hours_from_secs(record['out_timestamp']-record['in_timestamp'])
        
        res = await db.store_new_historical(ctx.guild.id, record)
        
        if not res:
            return {'status': False, 'record': record, 'row': None, 'content': f'Failed to store record to historical, contact admin\n{found}'}
        tot = await db.get_user_hours(ctx.guild.id, user_id)
        user = await ctx.guild.fetch_member(user_id)
        return {'status': True,'record': record, 'row': res, 'content': f'{user.display_name} {com.scram("Successfully")} clocked out at <t:{record["out_timestamp"]}>, stored record #{res} for {record["_DEBUG_delta"]} hours. Your total is at {round(tot, 2)}'}
    
    # ==============================================================================
    # Session Commands
    # ==============================================================================
    @get_group.command(name='session', description='Ephemeral - Get information about the active session')
    @is_member()
    async def _getsession(self, ctx):
        session = await db.get_session(ctx.guild.id)
        if not session:
            content = f'There is no active session right now.'
            await ctx.send_response(content=content, ephemeral=True)
            return
        start_timestamp = session["start_timestamp"]
        content = f'Session \"{session["session"]}\" started at <t:{start_timestamp}:f> local'
        await ctx.send_response(content=content, ephemeral=True)
        return
    
    @session_group.command(name='start', description='Start an session, only one session is allowed at a time')
    @is_member()
    @is_member_visible()
    @is_command_channel()
    async def _sessionstart(self, ctx, sessionname: discord.Option(str, name="session_name", required=True)):
        content = f"I'm busy updating, please try again later"
        await self.state_lock.acquire()
        try:
            session = await db.get_session(ctx.guild.id)
            if not session:
                now = com.get_current_datetime()
                session = {
                           'session': sessionname,
                           'created_by': ctx.author.id,
                           'ended_by': '',
                           'start_timestamp': int(now.timestamp()),
                           'end_timestamp': 0,
                           '_DEBUG_start': now.isoformat(),
                           '_DEBUG_started_by': ctx.author.name,
                           '_DEBUG_end': '',
                           '_DEBUG_ended_by': '',
                           '_DEBUG_delta': '',
                           }
                row = await db.set_session(ctx.guild.id, session)
                content = f'Session {session["session"]} started at <t:{session["start_timestamp"]}:f> - {row}'
                if not row:
                    content = f'Session start failed session names must be unique, try again or contact an administrator'
            else:
                content = f'Sorry, a session, {session["session"]}, is already in place, please end the session before starting a new one'
        finally:
            self.state_lock.release()
        await ctx.send_response(content=content)
    
    @session_group.command(name='end', description='Ends active session, clocking out all active users in the process')
    @is_member()
    @is_member_visible()
    @is_command_channel()
    async def _sessionend(self, ctx):
        content = f"I'm busy updating, please try again later"
        await self.state_lock.acquire()
        try:
            session = await db.get_session(ctx.guild.id)
            if session:
                now = com.get_current_datetime()
                session['ended_by'] = ctx.author.id
                session['end_timestamp'] = int(now.timestamp())
                session['_DEBUG_end'] = now.isoformat()
                session['_DEBUG_ended_by'] = ctx.author.name
                session['_DEBUG_delta'] = com.get_hours_from_secs(session['end_timestamp'] - 
                                                              session['start_timestamp'])
                
                actives = await db.get_all_actives(ctx.guild.id)
                close_outs = []
                fails = []
                
                for active in actives:
                    res = await self._inner_clockout(ctx, active["user"])
                    close_outs.append((res['record']['_DEBUG_user_name'], res['record']['_DEBUG_delta']))
                    if not res['status']:
                        fails.append(active)
                        continue
                    bonus_sessions = await self.get_bonus_sessions(ctx.guild.id, res['record'], res['row'])
                    for item in bonus_sessions:
                        row = await db.store_new_historical(ctx.guild.id, item)
                        close_outs.append((item['_DEBUG_user_name'], f'Bonus id#{row}', item['_DEBUG_delta']))
                content = f'Session, "{session["session"]}" ended and lasted {session["_DEBUG_delta"]} hours'
                if close_outs:       
                    content += f'\nAutomagically closed out {close_outs}'
                if fails:
                    content += f'\nFailed to close out record {fails}, contact administrator'
                
                await db.store_historical_session(ctx.guild.id, session)
                await db.delete_session(ctx.guild.id)
                await db.clear_replacement_queue(ctx.guild.id)
            else:
                content=f'Sorry there is no current session to end'
        finally:
            self.state_lock.release()
        await ctx.send_response(content=content)
    
    # ==============================================================================
    # Utility/Fetch Commands
    # ==============================================================================
    
    @commands.slash_command(name='list', description='Ephemeral optional - Gets list of users that have accrued time, ordered by highest hours urned')
    @is_member()
    async def _list(self, ctx, public: discord.Option(bool, name='public', default=False)):
        # List all users in ranked order
        # get unique users
        users = await db.get_unique_users(ctx.guild.id)
        
        res = await db.get_users_hours(ctx.guild.id, users)
        
        sorted_res = sorted(res, key= lambda user: user['total'], reverse=True)
        content_container = []
        content = '_ _\nUsers sorted by total time:'
        for idx, item in enumerate(sorted_res):
            content += f'\n#{idx+1} <@{item["user"]}> has {item["total"]:.2f}'
            if len(content) >= 1850:
                clip_idx = content.rfind('\n', 0, 1850)
                content_container.append(content[:clip_idx])
                content = '_ _'+content[clip_idx:]
        if sorted_res:
            content_container.append(content)
        
        await ctx.send_response(content=content_container[0], ephemeral=not public, allowed_mentions=discord.AllowedMentions(users=False))
        if len(content_container) > 1:
            for idx in range(1, len(content_container)):
                await ctx.send_followup(content=content_container[idx], ephemeral=not public, allowed_mentions=discord.AllowedMentions(users=False))
        return
    
    @commands.slash_command(name='urn', description='For use when you have obtained an urn')
    @is_member()
    @is_member_visible()
    @is_command_channel()
    async def _urn(self, ctx):
        actives = await db.get_all_actives(ctx.guild.id)
        for active in actives:
            if active['user'] == ctx.author.id:
                await ctx.send_response(content=f'Please clock out before attempting to claim your Urn')
                return
        view = ClearOutView()
        await ctx.respond("Did you really get an URN!?! Are you ready to clear out your dkp/time to 0?", view=view)
        await view.wait()
        if view.result == None:
            # Time out
            return
        elif view.result == True:
            tot = await db.get_user_seconds(ctx.guild.id, ctx.author.id)
            session = await db.get_session(ctx.guild.id)
            session_name = ''
            if session:
                session_name = session['session']
            now = com.get_current_datetime()
            hours = com.get_hours_from_secs(tot)
            doc = {
                'user': ctx.author.id,
                'character': f"URN_ZERO_OUT_EVENT -{hours}",
                'session': session_name,
                'in_timestamp': int(now.timestamp()),
                'out_timestamp': (now.timestamp())-tot,
                '_DEBUG_user_name': ctx.author.display_name,
                '_DEBUG_in': now.isoformat(),
                '_DEBUG_out': now.isoformat(),
                '_DEBUG_delta': -1*hours,
            }
            res = await db.store_new_historical(ctx.guild.id, doc)
            if not res:
                print(f"Clearout failure\n {doc}", flush=True)
            await view.message.edit(content=f"Ooooh, yes! :urn: :tada: {hours} hours well spent!")
            return
        else:
            # User Aborted
            return
    
    # return last # commands
    @get_group.command(name='commands', description='Ephemeral - Get a list of historical commands submitted to the bot by a user')
    @is_member()
    async def _get_commands(self, ctx, 
                            _id: discord.Option(str, name="user_id", default=None),
                            startat: discord.Option(int, name="start_at", default=0), 
                            count: discord.Option(int, name="count", default=10)):
        if _id is None:
            userid = ctx.author.id
        else:
            userid = await check_user_id(ctx, _id)
            if userid is None:
                return
                
        res = await db.get_user_commands_history(ctx.guild.id, userid, start_at=int(startat), count=int(count))
        content = f"<@{userid}>'s last {len(res)} commands"
        if startat:
            content += f", starting at user's {startat}'th most recent command"
        if res:
            content += '```'
        for item in res:
            del item['server']
            del item['user']
            del item['user_name']
            if item['options'] == 'None':
                del item['options']
            content += f"\n{str(item)}"
        content = content[:1990]
        if res:
            content += '```'
        await ctx.send_response(content=content, ephemeral=True)
        pass
    
    '''
    # Gets last 20 commands by user, returned as an ephemeral message or maybe all commands as an attached doc?
    @commands.user_command(name="Get User Commands")
    @is_member()
    async def _get_user_commands(self, ctx, member: discord.Member):
        res = await db.get_user_commands_history(ctx.guild.id, member.id)
        content = f"<@{member.id}>'s last {len(res)} commands"
        if res:
            content += '```'
        for item in res:
            del item['server']
            del item['user']
            del item['user_name']
            if item['options'] == 'None':
                del item['options']
            content += f"\n{str(item)}"
        
        content = content[:1990]
        if res:
            content += '```'
        await ctx.send_response(content=content, ephemeral=True)
        pass
    '''
    
    # Gets last 20 commands by user, returned as an ephemeral message or maybe all commands as an attached doc?
    @commands.user_command(name="Get User Time")
    @is_member()
    async def _get_user_time(self, ctx, member: discord.Member):
        secs = await db.get_user_seconds(ctx.guild.id, member.id)
        tot = com.get_hours_from_secs(secs)
        await ctx.send_response(content=f'{member.display_name} has accrued {tot:.2f} hours. ({secs} seconds)', ephemeral=True)
    
    @get_group.command(name="usersessions", description='Ephemeral - Get list of user\'s historical sessions')
    @is_member()
    async def _cmd_get_user_sessions(self, ctx, 
                                    _id: discord.Option(str, name="user_id", default=None),
                                    _timetype: discord.Option(str, name="timetype", choices=["Hours", "Seconds"], default='Hours'),
                                    _public: discord.Option(bool, name="public", default=False)):
        
        if _id is None:
            userid = ctx.author.id
        else:
            userid = await check_user_id(ctx, _id)
            if userid is None:
                return
        
        res = await db.get_historical_user(ctx.guild.id, userid)
        if len(res) == 0:
            await ctx.send_response(content=f"{member.display_name} has no recorded sessions", ephemeral=True)
            return
        chunks = []
        title = f"_ _\n<@{userid}> Sessions:\n"
        content = ""
        for item in res:
            _in = com.datetime_from_timestamp(item['in_timestamp'])
            _out = com.datetime_from_timestamp(item['out_timestamp'])
            ses_hours = "Null"
            if _timetype == 'Hours':
                ses_hours = com.get_hours_from_secs(item['out_timestamp'] - item['in_timestamp'])
            elif _timetype == 'Seconds':
                ses_hours = item['out_timestamp'] - item['in_timestamp']
            catagory = "  "
            if "_PCT_BONUS_" in item['character']:
                catagory = " +"
            elif item['character'].startswith("URN_ZERO_OUT_EVENT"):
                catagory = "⚱️"
            elif item['character'] == "SOLO_HOLD_BONUS":
                catagory = " S"
            elif item['character'] == "QUAKE_DS_BONUS":
                catagory = " Q"
            tz = com.get_timezone_str()
            content += f"\n{item['rowid']:5} {_in.date().isoformat()} - {item['session'][:50]:50}  {catagory} from {_in.time()} {_in.strftime('%Z')} to {_out.time()} {_out.strftime('%Z')} for {ses_hours} {_timetype.lower()}"
            # Max message length is 2000, give 100 leway for title/user hours ending
            if len(content) >= 1850:
                clip_idx = content.rfind('\n', 0, 1850)
                chunks.append(content[:clip_idx])
                content = content[clip_idx:]
        
        secs = await db.get_user_seconds(ctx.guild.id, userid)
        tot = com.get_hours_from_secs(secs)
        tail = f"\n<@{userid}> has accrued {tot} hours. ({secs} seconds)"        
        if res:
            chunks.append(content)
        
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                content = title+"```"+chunk+"```"
                if len(chunks) == 1:
                    content += tail
                await ctx.send_response(content=content, ephemeral=not _public, allowed_mentions=discord.AllowedMentions(users=False))
            elif len(chunks) == idx+1:
                await ctx.send_followup(content="```"+chunk+"```"+tail, ephemeral=not _public, allowed_mentions=discord.AllowedMentions(users=False))
            else:
                await ctx.send_followup(content="```"+chunk+"```", ephemeral=not _public, allowed_mentions=discord.AllowedMentions(users=False))
    
    #TODO condense this with slash command of same name
    @commands.user_command(name="Get User Sessions")
    @is_member()
    async def _get_user_sessions(self, ctx, member: discord.Member):
        await self._cmd_get_user_sessions(ctx, member.id, "Hours", False)
        return
    '''
    @commands.slash_command(name="getuserseconds", description='Get total number of seconds that a user has accrued')
    @is_member()
    async def _get_user_seconds(self, ctx,  _id: discord.Option(str, name="user_id", default='')):
        if _id is None:
            userid = ctx.author.id
        else:
            userid = await check_user_id(ctx, _id)
            if userid is None:
                return
        
        secs = await db.get_user_seconds(ctx.guild.id, userid)
        await ctx.send_response(content=f'<@{userid}> has {secs}', ephemeral=True)
    '''
    # ==============================================================================
    # Admin functions
    # ==============================================================================   
    @admin_group.command(name="testcommand", description='Command to confirm the user is an admin')
    @is_admin()
    async def _admincommand(self, ctx):
        await ctx.send_response(content=f'You\'re an admin!')
    
    @admin_group.command(name='directurn', description='Directly /urn a user')
    @is_admin()
    @is_member()
    @is_member_visible()
    async def _directurn(self, ctx, 
                        sessionname: discord.Option(str, name="sessionname", required=True),
                        _id: discord.Option(str, name="userid", required=True),
                        username: discord.Option(str, name="username", required=True),
                        date: discord.Option(str, name="killdate", description="Form YYYY-MM-DD", required=True),
                        time: discord.Option(str, name="killtime", description="Form HH:MM in EST", required=True)):
        
        userid = await check_user_id(ctx, _id)
        if userid is None:
            return
        secs = await db.get_user_seconds(ctx.guild.id, userid)
        hours = com.get_hours_from_secs(secs)
        datetime_kill = com.datetime_from_iso(date+"T"+time+":00-05:00")
        rev_timestamp = datetime_kill.timestamp() - secs
        rev_datetime = com.datetime_from_timestamp(rev_timestamp)
        doc = {
                'user': int(userid),
                'character': f"URN_ZERO_OUT_EVENT -{hours}",
                'session': sessionname,
                'in_timestamp': int(datetime_kill.timestamp()),
                'out_timestamp': int(rev_timestamp),
                '_DEBUG_user_name': username,
                '_DEBUG_in': datetime_kill.isoformat(),
                '_DEBUG_out': rev_datetime.isoformat(),
                '_DEBUG_delta': -1*hours,
            }
        try:
            res = await db.store_new_historical(ctx.guild.id, doc)
        except OperationalError as err:
            await ctx.send_response(content=f'Failed, database error - {err}, please try again or contact an administator')
            return
        if not res:
            await ctx.send_response(content=f'Something went wrong, return index 0 please contact an administator')
            return
        tot = await db.get_user_hours(ctx.guild.id, int(userid))
        
        await ctx.send_response(content=f'{username} - <@{int(userid)}> {com.scram("Successfully")} URNed and stored record #{res} for {doc["_DEBUG_delta"]} hours. Total is at {tot}')
    
    @admin_group.command(name='changehistory', description='Change a historical record of a user')
    @is_admin()
    @is_member()
    @is_member_visible()
    async def _adminchangehistory(self, ctx,
                                  row: discord.Option(str, name="recordnumber", required=True),
                                  _type: discord.Option(str, name="type", choices=['Clock in time', 'Clock out time'], required=True),
                                  _date: discord.Option(str, name="date", description="Form YYYY-MM-DD", required=True),
                                  time: discord.Option(str, name="time", description="24 hour clock, 12pm midnight is 00:00", required=True)):
            
        rec = await db.get_historical_record(ctx.guild.id, row)
        
        if len(rec) == 0 or len(rec) > 1:
            await ctx.send_response(content=f'Could not find record #{row} for guild {ctx.guild.id}')
            return
        rec = rec[0]
        
        was = {}
        if len(time) == 4:
            time = "0" + time
        _datetime = com.datetime_combine(_date, time)
        if _type == 'Clock in time':
            was['timestamp'] = rec['in_timestamp']
            was['_DEBUG'] = rec['_DEBUG_in']
            rec['in_timestamp'] = _datetime.timestamp()
            rec['_DEBUG_in'] = _datetime.isoformat()
            
        elif _type == 'Clock out time':
            was['timestamp'] = rec['out_timestamp']
            was['_DEBUG'] = rec['_DEBUG_out']
            rec['out_timestamp'] = _datetime.timestamp()
            rec['_DEBUG_out'] = _datetime.isoformat()
        else:
            await ctx.send_response(content=f'Invalid option {_type}')
            return
            
        rec['_DEBUG_delta'] = com.get_hours_from_secs(rec['out_timestamp']-rec['in_timestamp'])    
        res = await db.delete_historical_record(ctx.guild.id, row)
        res = await db.store_new_historical(ctx.guild.id, rec)
        await ctx.send_response(content=f'Updated record #{row}, {_type} from {was["_DEBUG"]} to {_datetime.isoformat()} for user <@{rec["user"]}>', allowed_mentions=discord.AllowedMentions(users=False))
    
    @admin_group.command(name='directrecord', description='Add a historical record for a user')
    @is_admin()
    @is_member()
    @is_member_visible()
    async def _directrecord(self, ctx,  
                            sessionname: discord.Option(str, name="sessionname", required=True),
                            userid: discord.Option(str, name="userid", required=True),
                            username: discord.Option(str, name="username", required=True),
                            date: discord.Option(str, name="startdate", description="Form YYYY-MM-DD", required=True),
                            intime: discord.Option(str, name="intime", description="Form HH:MM in EST", required=True),
                            outtime: discord.Option(str, name="outtime", description="Form HH:MM in EST", required=True),
                            character: discord.Option(str, name="character", default=''),
                            dayafter: discord.Option(str, name="dayafter", choices=['True', 'False'], description="Did clockout occur the day after in?", default='False')):
        userid = await check_user_id(ctx, userid)
        if userid is None:
            return
        if len(intime) == 4:
            intime = "0" + intime
        if len(outtime) == 4:
            outtime = "0" + outtime
        intime += "-05:00"
        outtime += "-05:00"
        arg_date = datetime.date.fromisoformat(date)
        in_datetime = com.datetime_combine(arg_date.isoformat(), intime)
        if dayafter == "True":
            out_datetime = com.datetime_combine((arg_date+datetime.timedelta(days=1)).isoformat(), outtime)
        else:
            out_datetime = com.datetime_combine(arg_date.isoformat(), outtime)
        in_timestamp = int(in_datetime.timestamp())
        out_timestamp = int(out_datetime.timestamp())
        doc = {
                'user': int(userid),
                'character': character,
                'session': sessionname,
                'in_timestamp': in_timestamp,
                'out_timestamp': out_timestamp,
                '_DEBUG_user_name': username,
                '_DEBUG_in': in_datetime.isoformat(),
                '_DEBUG_out': out_datetime.isoformat(),
                '_DEBUG_delta': com.get_hours_from_secs(out_timestamp-in_timestamp),
            }
        try:
            res = await db.store_new_historical(ctx.guild.id, doc)
        except OperationalError as err:
            await ctx.send_response(content=f'Failed, database error - {err}, please try again or contact an administator')
            return
        if not res:
            await ctx.send_response(content=f'Something went wrong, return index 0 please contact an administator')
            return
        tot = await db.get_user_hours(ctx.guild.id, int(userid))
        await ctx.send_response(content=f'{username} - <@{int(userid)}> {com.scram("Successfully")} clocked out and stored record #{res} for {doc["_DEBUG_delta"]} hours. Total is at {tot}')
    
    # ==============================================================================
    # Data functions
    # ==============================================================================
    
    @get_group.command(name='data', description='Command to retrive all data of a table')
    @is_member()
    async def _getdata(self, ctx, data_type=discord.Option(name='datatype', choices=['actives','historical','session', 'historicalsession', 'commands', 'errors'], default='historical')):
        res = await db.flush_wal()
        if not res:
            await ctx.send_response(content='Couldn\'t flush journal, possible multiple connections active, contact administrator')
            return
        if data_type == 'historical':
            data = await db.get_historical(ctx.guild.id)
        elif data_type == 'actives':
            data = await db.get_all_actives(ctx.guild.id)
        elif data_type == 'session':
            data = [await db.get_session(ctx.guild.id)]
        elif data_type == 'commands':
            data = await db.get_commands_history(ctx.guild.id)
        else:
            await ctx.send_response(content='Option not available yet')
            return
        out_file = Path('/temp/data.json')
        out_file.parent.mkdir(exist_ok=True, parents=True)
        json.dump(data, open('temp/data.json', 'w', encoding='utf-8'), indent=1)
        await ctx.send_response(content='Here\'s the data!', file=discord.File('temp/data.json', filename='data.json'))
        return
    
    def get_config(self, guild_id):
        return json.load(open('data/config.json', 'r', encoding='utf-8')).get(str(guild_id))
    

# function to accept a user id to check, or partial/full string to match user name on, returns None on didnt find or an userid int
async def check_user_id(ctx, param) -> int:
    # Try userid for int interpretation
    ret = {'result': None, 'type': MemberQueryResult.QUERY_FAILED}
    try:
        int(param)
        res = await ctx.guild.fetch_member(int(param))
        ret = {'result': res, 'type': MemberQueryResult.FOUND}
    except (ValueError, TypeError) as err:
        # Failed int parsing
        pass 
    except discord.errors.NotFound:
        ret = {'result': None, 'type': MemberQueryResult.ID_NOT_FOUND}
    
    if not ret['result']:
        # try querying string for member
        try:
            res = await ctx.guild.query_members(query=param, limit=2)
            if len(res) == 0:
                ret = {'result': None, 'type': MemberQueryResult.ID_NOT_FOUND}
            elif len(res) == 1:
                ret = {'result': res[0], 'type': MemberQueryResult.FOUND}
            else:
                ret = {'result': None, 'type': MemberQueryResult.NOT_UNIQUE}
        except Exception as err:
            pass
    
    if ret['result'] is None:
        await ctx.send_response(content=f"userid '{param}' couldnt be found, returned {ret['type']}", ephemeral=True)
        return None
    return ret['result'].id

def setup(bot):
    cog = Clocks(bot)
    
    bot.add_cog(Clocks(bot))