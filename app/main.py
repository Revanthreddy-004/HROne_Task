import os, re, math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://127.0.0.1:27017'), serverSelectionTimeoutMS=2000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]
IST = timezone(timedelta(hours=5, minutes=30)); UTC=timezone.utc
PRESENCE={'PRESENT','WFH','ON_DUTY'}; STATUSES={'PRESENT','ABSENT','LEAVE','WFH','ON_DUTY'}
app=FastAPI(title='Employee Attendance & Analytics API', version='2.0.0')

@app.on_event('startup')
def indexes():
    db.employees.create_index([('emp_code',ASCENDING)],unique=True,name='uq_employee_code')
    db.employees.create_index([('department',ASCENDING),('joined_on',ASCENDING)],name='department_joined')
    db.attendance_logs.create_index([('emp_code',ASCENDING),('date',ASCENDING)],unique=True,name='uq_attendance_employee_date')
    db.attendance_logs.create_index([('date',DESCENDING),('emp_code',ASCENDING)],name='attendance_date_emp')
    db.attendance_logs.create_index([('date',ASCENDING),('status',ASCENDING),('emp_code',ASCENDING)],name='attendance_date_status_emp')

def bad(msg): raise HTTPException(422,msg)
def valid_date(s):
    try:
        if not isinstance(s,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',s): raise ValueError()
        return date.fromisoformat(s)
    except Exception: bad('date must be YYYY-MM-DD')
def valid_month(s):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',s or ''): bad('month must be YYYY-MM')
    y,m=map(int,s.split('-')); return date(y,m,1), (date(y+1,1,1) if m==12 else date(y,m+1,1))
def epoch_dt(ms):
    if type(ms) is not int or ms < 100000000000 or ms > 4102444800000: bad('timestamp must be epoch milliseconds integer')
    return datetime.fromtimestamp(ms/1000,UTC).replace(microsecond=0).replace(tzinfo=None)
def dt_ms(d):
    if d is None:return None
    if d.tzinfo is None:d=d.replace(tzinfo=UTC)
    return int(d.timestamp()*1000)
def as_utc(d):
    if d is None:return None
    if d.tzinfo is None:return d.replace(tzinfo=UTC)
    return d.astimezone(UTC)
def ist_dt(d): return as_utc(d).astimezone(IST)
def floor2(x): return float(Decimal(str(x)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))
def floor4(x): return float(Decimal(str(x)).quantize(Decimal('0.0001'),rounding=ROUND_HALF_UP))
def shift_time(s):
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d',s or ''): bad('shift times must be HH:MM')
    h,m=map(int,s.split(':')); return time(h,m)
def shift_boundary(day, hhmm, next_day=False):
    d=day+timedelta(days=1) if next_day else day
    t=shift_time(hhmm); return datetime.combine(d,t,IST).astimezone(UTC).replace(tzinfo=None)
def attendance_day(punch, emp):
    local=ist_dt(punch); d=local.date()
    if emp['shift_end'] <= emp['shift_start'] and local.time().replace(tzinfo=None) < shift_time(emp['shift_end']): d-=timedelta(days=1)
    return d.isoformat()
def calc_late(pin, emp, day):
    if pin is None:return 0
    start=shift_boundary(date.fromisoformat(day),emp['shift_start'])
    mins=int((pin-start).total_seconds()//60)
    return mins if (pin-start).total_seconds()>600 else 0
def calc_out(pin,pout,emp,day):
    if pin is None or pout is None:return None,0,False
    hours=floor2((pout-pin).total_seconds()/3600)
    overnight=emp['shift_end']<=emp['shift_start']
    end=shift_boundary(date.fromisoformat(day),emp['shift_end'],overnight)
    delta=(pout-end).total_seconds()
    ot=int(delta//60) if delta>=1800 else 0
    return hours,ot,hours<4.5
def serialize_employee(d):
    d=dict(d); d.pop('_id',None); d['created_at']=dt_ms(d.get('created_at')); return d
def serialize_att(d):
    d=dict(d); d.pop('_id',None)
    for k in ('punch_in','punch_out'):
        d[k]=dt_ms(d.get(k))
    d.setdefault('work_hours',None);d.setdefault('late_minutes',0);d.setdefault('overtime_minutes',0);d.setdefault('half_day',False);d.setdefault('history',[])
    hist=[]
    for h in d['history']:
        h=dict(h);h['at']=dt_ms(h.get('at'))
        for fld,change in list(h.get('changes',{}).items()):
            if fld in ('punch_in','punch_out'):
                change=dict(change);change['from']=dt_ms(change.get('from'));change['to']=dt_ms(change.get('to'));h['changes'][fld]=change
        hist.append(h)
    d['history']=hist; return d
class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str=Field(pattern=r'^EMP\d{4,6}$'); name:str=Field(min_length=1,max_length=100); email:str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$'); department:str=Field(min_length=1,max_length=50); shift_start:str='09:30';shift_end:str='18:30';joined_on:str
    @field_validator('shift_start','shift_end')
    @classmethod
    def times(cls,v): shift_time(v);return v
    @field_validator('joined_on')
    @classmethod
    def joined(cls,v): valid_date(v);return v
    @model_validator(mode='after')
    def shifts(self):
        if self.shift_start==self.shift_end: raise ValueError('shift_start must differ from shift_end')
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str; punched_at:Optional[int]=None;status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @field_validator('punched_at')
    @classmethod
    def stamp(cls,v):
        if v is not None: epoch_dt(v)
        return v
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str;punched_at:Optional[int]=None
    @field_validator('punched_at')
    @classmethod
    def stamp(cls,v):
        if v is not None: epoch_dt(v)
        return v
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None
    punch_in:Optional[int]=None;punch_out:Optional[int]=None
    reason:str=Field(min_length=5,max_length=200);regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator('punch_in','punch_out')
    @classmethod
    def stamps(cls,v):
        if v is not None:epoch_dt(v)
        return v

@app.get('/health')
def health():
    try: client.admin.command('ping');return {'status':'ok'}
    except Exception: raise HTTPException(503,'MongoDB unavailable')
@app.post('/employees',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump();doc['created_at']=datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    return serialize_employee(doc)
@app.get('/employees')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {};total=db.employees.count_documents(q)
    items=[serialize_employee(x) for x in db.employees.find(q,{'_id':0}).sort('emp_code',ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {'items':items,'total':total,'page':page,'page_size':page_size}
@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp:raise HTTPException(404,'employee not found')
    ts=epoch_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    day=attendance_day(ts,emp);doc={'emp_code':body.emp_code,'date':day,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':calc_late(ts,emp,day),'overtime_minutes':0,'half_day':False,'history':[]}
    try:db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:raise HTTPException(409,'already punched in for this date')
    return serialize_att(doc)
@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp:raise HTTPException(404,'employee not found')
    pout=epoch_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    # Most recent open record whose punch-in is no later than punch-out; natural date order supports overnight shifts.
    rec=db.attendance_logs.find_one({'emp_code':body.emp_code,'status':{'$in':list(PRESENCE)},'punch_in':{'$lte':pout}},sort=[('punch_in',DESCENDING)])
    if not rec:raise HTTPException(404,'no punch-in found')
    if rec.get('punch_out') is not None:raise HTTPException(409,'already punched out')
    pin=rec['punch_in'];seconds=(pout-pin).total_seconds()
    if seconds<=0 or seconds>86400:bad('punched_at must be after punch_in and within 24 hours')
    hours,ot,half=calc_out(pin,pout,emp,rec['date'])
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],'punch_out':None},{'$set':{'punch_out':pout,'work_hours':hours,'overtime_minutes':ot,'half_day':half}},return_document=ReturnDocument.AFTER)
    if not updated:raise HTTPException(409,'already punched out')
    return serialize_att(updated)
@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code:q['emp_code']=emp_code
    if status:q['status']=status
    if date_from:valid_date(date_from)
    if date_to:valid_date(date_to)
    if date_from and date_to and date_from>date_to:bad('date_from must be <= date_to')
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from
        if date_to:q['date']['$lte']=date_to
    total=db.attendance_logs.count_documents(q);docs=db.attendance_logs.find(q,{'_id':0}).sort([('date',DESCENDING),('emp_code',ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {'items':[serialize_att(d) for d in docs],'total':total,'page':page,'page_size':page_size}
@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:RegularizeIn):
    valid_date(date); emp=db.employees.find_one({'emp_code':emp_code})
    if not emp:raise HTTPException(404,'employee not found')
    old=db.attendance_logs.find_one({'emp_code':emp_code,'date':date})
    if not old:raise HTTPException(404,'attendance record not found')
    supplied=body.model_fields_set; new=dict(old)
    if body.status is not None:new['status']=body.status
    for fld in ('punch_in','punch_out'):
        if fld in supplied:new[fld]=epoch_dt(getattr(body,fld)) if getattr(body,fld) is not None else None
    if new['status'] in ('ABSENT','LEAVE'):
        if ('punch_in' in supplied and body.punch_in is not None) or ('punch_out' in supplied and body.punch_out is not None):bad('ABSENT/LEAVE cannot have punch times')
        new['punch_in']=new['punch_out']=None
    else:
        if new.get('punch_in') is None:bad('presence status requires punch_in')
        if attendance_day(new['punch_in'],emp)!=date:bad('punch_in must belong to attendance date')
        if new.get('punch_out') is not None:
            secs=(new['punch_out']-new['punch_in']).total_seconds()
            if secs<=0 or secs>86400:bad('punch_out must be after punch_in and within 24 hours')
    new['late_minutes']=calc_late(new.get('punch_in'),emp,date) if new['status'] in PRESENCE else 0
    if new['status'] not in PRESENCE:new.update(work_hours=None,overtime_minutes=0,half_day=False)
    elif new.get('punch_out') is None:new.update(work_hours=None,overtime_minutes=0,half_day=False)
    else:
        new['work_hours'],new['overtime_minutes'],new['half_day']=calc_out(new['punch_in'],new['punch_out'],emp,date)
    fields=['status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day'];changes={}
    for f in fields:
        a=old.get(f);b=new.get(f)
        if a!=b:changes[f]={'from':a,'to':b}
    if not changes:bad('request does not change the record')
    hist=list(old.get('history',[]));hist.append({'at':datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None),'by':body.regularized_by,'reason':body.reason,'changes':changes})
    sets={f:new.get(f) for f in fields};sets['history']=hist
    updated=db.attendance_logs.find_one_and_update({'_id':old['_id'],'history':old.get('history',[])},{'$set':sets},return_document=ReturnDocument.AFTER)
    if not updated:raise HTTPException(409,'record changed concurrently; retry')
    return serialize_att(updated)

# Analytics are expressed as MongoDB aggregations. Python only computes calendar boundaries and serializes results.
def month_dates(month):
    start,end=valid_month(month);return start.isoformat(),end.isoformat(),(end-timedelta(days=1)).isoformat()
def working_days(joined,start,end):
    a=max(date.fromisoformat(joined),date.fromisoformat(start));b=date.fromisoformat(end)
    if a>b:return 0
    return sum(1 for i in range((b-a).days+1) if (a+timedelta(days=i)).weekday()<5)
def present_expr():return {'$in':['$status',list(PRESENCE)]}
def present_value_expr():return {'$cond':[{'$and':[present_expr(),{'$eq':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},1]}]},0,0]}
@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str):
    start,end,last=month_dates(month);emp=db.employees.find_one({'emp_code':emp_code})
    if not emp:raise HTTPException(404,'employee not found')
    logs=list(db.attendance_logs.aggregate([{'$match':{'emp_code':emp_code,'date':{'$gte':start,'$lt':end}}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$in':['$status',list(PRESENCE)]},{'$cond':[{'$lt':[{'$ifNull':['$work_hours',999]},4.5]},0.5,1]},0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]))
    v=logs[0] if logs else {};wd=working_days(emp['joined_on'],max(start,emp['joined_on']),last)
    # Present days count only Mon-Fri, so reaggregate with weekday filter.
    weekday_logs=list(db.attendance_logs.aggregate([{'$match':{'emp_code':emp_code,'date':{'$gte':max(start,emp['joined_on']),'$lt':end}}},{'$addFields':{'dow':{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}}}},{'$match':{'dow':{'$gte':2,'$lte':6}}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$in':['$status',list(PRESENCE)]},{'$cond':[{'$lt':[{'$ifNull':['$work_hours',999]},4.5]},0.5,1]},0]}}}}]))
    pd=weekday_logs[0]['present_days'] if weekday_logs else 0
    return {'emp_code':emp_code,'month':month,'working_days':wd,'present_days':floor2(pd),'leave_days':v.get('leave_days',0),'late_count':v.get('late_count',0),'total_late_minutes':v.get('total_late_minutes',0),'total_overtime_minutes':v.get('total_overtime_minutes',0),'attendance_pct':None if wd==0 else floor2(pd/wd*100)}

@app.get('/analytics/departments/summary')
def department_summary(month: str, department: Optional[str] = None):
    start, end, last = month_dates(month)

    # Aggregate attendance metrics by department.
    pipeline = [
        {
            '$match': {
                'date': {'$gte': start, '$lt': end}
            }
        },
        {
            '$lookup': {
                'from': 'employees',
                'localField': 'emp_code',
                'foreignField': 'emp_code',
                'as': 'emp'
            }
        },
        {'$unwind': '$emp'}
    ]

    if department is not None:
        pipeline.append({
            '$match': {'emp.department': department}
        })

    pipeline.append({
        '$group': {
            '_id': '$emp.department',
            'present_days': {
                '$sum': {
                    '$cond': [
                        {
                            '$and': [
                                {'$in': ['$status', list(PRESENCE)]},
                                {
                                    '$gte': [
                                        {
                                            '$dayOfWeek': {
                                                '$dateFromString': {
                                                    'dateString': '$date'
                                                }
                                            }
                                        },
                                        2
                                    ]
                                },
                                {
                                    '$lte': [
                                        {
                                            '$dayOfWeek': {
                                                '$dateFromString': {
                                                    'dateString': '$date'
                                                }
                                            }
                                        },
                                        6
                                    ]
                                }
                            ]
                        },
                        {
                            '$cond': [
                                {
                                    '$lt': [
                                        {'$ifNull': ['$work_hours', 999]},
                                        4.5
                                    ]
                                },
                                0.5,
                                1
                            ]
                        },
                        0
                    ]
                }
            },
            'hours': {
                '$avg': {
                    '$cond': [
                        {
                            '$and': [
                                {'$in': ['$status', list(PRESENCE)]},
                                {'$ne': ['$work_hours', None]}
                            ]
                        },
                        '$work_hours',
                        None
                    ]
                }
            },
            'late_count': {
                '$sum': {
                    '$cond': [
                        {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]},
                        1,
                        0
                    ]
                }
            },
            'total_late_minutes': {
                '$sum': {'$ifNull': ['$late_minutes', 0]}
            },
            'leave_count': {
                '$sum': {
                    '$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]
                }
            },
            'on_duty_count': {
                '$sum': {
                    '$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]
                }
            }
        }
    })

    aggs = {
        item['_id']: item
        for item in db.attendance_logs.aggregate(pipeline)
    }

    # Calculate department headcounts inside MongoDB.
    headcount_pipeline = [
        {
            '$match': {
                'joined_on': {'$lt': end},
                **({'department': department} if department is not None else {})
            }
        },
        {
            '$group': {
                '_id': '$department',
                'headcount': {'$sum': 1}
            }
        }
    ]

    heads = {
        item['_id']: item['headcount']
        for item in db.employees.aggregate(headcount_pipeline)
    }

    # Preserve the existing response structure.
    items = []

    for dep, count in sorted(heads.items()):
        if not count:
            continue

        metrics = aggs.get(dep, {})

        items.append({
            'department': dep,
            'headcount': count,
            'present_days': floor2(metrics.get('present_days', 0)),
            'avg_work_hours': (
                None
                if metrics.get('hours') is None
                else floor2(metrics['hours'])
            ),
            'late_count': metrics.get('late_count', 0),
            'total_late_minutes': metrics.get('total_late_minutes', 0),
            'leave_count': metrics.get('leave_count', 0),
            'on_duty_count': metrics.get('on_duty_count', 0)
        })

    return {
        'month': month,
        'items': items
    }

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end,_=month_dates(month);match={'date':{'$gte':start,'$lt':end},'late_minutes':{'$gt':0}}
    p=[{'$match':match},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'},'late_count':{'$sum':1}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'emp'}},{'$unwind':'$emp'}]
    if department is not None:p.append({'$match':{'emp.department':department}})
    p += [{'$sort':{'total_late_minutes':-1,'_id':1}},{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$emp.name','department':'$emp.department','total_late_minutes':1,'late_count':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(p))}


@app.get('/analytics/departments/{department}/trend')
def department_trend(
    department: str,
    from_: str = Query(alias='from'),
    to: str = Query(...)
):
    a = valid_date(from_)
    b = valid_date(to)

    if b < a or (b - a).days + 1 > 92:
        bad('date range must be ordered and no longer than 92 days')

    if not db.employees.find_one({'department': department}):
        raise HTTPException(404, 'department not found')

    start_dt = datetime.combine(a, time.min)
    end_dt = datetime.combine(b, time.min)
    day_count = (b - a).days + 1

    pipeline = [
        {'$documents': [{}]},
        {'$project': {
            'dates': {
                '$map': {
                    'input': {'$range': [0, day_count]},
                    'as': 'i',
                    'in': {
                        '$dateAdd': {
                            'startDate': start_dt,
                            'unit': 'day',
                            'amount': '$$i'
                        }
                    }
                }
            }
        }},
        {'$unwind': '$dates'},
        {'$project': {
            '_id': 0,
            'date_obj': '$dates',
            'date': {
                '$dateToString': {
                    'date': '$dates',
                    'format': '%Y-%m-%d',
                    'timezone': 'UTC'
                }
            }
        }},
        {'$lookup': {
            'from': 'employees',
            'let': {'ds': '$date'},
            'pipeline': [
                {'$match': {
                    '$expr': {
                        '$and': [
                            {'$eq': ['$department', department]},
                            {'$lte': ['$joined_on', '$$ds']}
                        ]
                    }
                }},
                {'$count': 'n'}
            ],
            'as': 'headcount_result'
        }},
        {'$lookup': {
            'from': 'attendance_logs',
            'let': {'ds': '$date'},
            'pipeline': [
                {'$match': {
                    '$expr': {'$eq': ['$date', '$$ds']}
                }},
                {'$lookup': {
                    'from': 'employees',
                    'localField': 'emp_code',
                    'foreignField': 'emp_code',
                    'as': 'emp'
                }},
                {'$unwind': '$emp'},
                {'$match': {'emp.department': department}},
                {'$group': {
                    '_id': None,
                    'present_count': {
                        '$sum': {
                            '$cond': [
                                {'$in': ['$status', list(PRESENCE)]},
                                {'$cond': [
                                    {'$lt': [
                                        {'$ifNull': ['$work_hours', 999]},
                                        4.5
                                    ]},
                                    0.5, 1
                                ]},
                                0
                            ]
                        }
                    },
                    'late_count': {
                        '$sum': {
                            '$cond': [
                                {'$gt': [
                                    {'$ifNull': ['$late_minutes', 0]},
                                    0
                                ]},
                                1, 0
                            ]
                        }
                    }
                }}
            ],
            'as': 'daily'
        }},
        {'$set': {
            'headcount': {
                '$ifNull': [
                    {'$arrayElemAt': ['$headcount_result.n', 0]},
                    0
                ]
            },
            'present_count': {
                '$ifNull': [
                    {'$arrayElemAt': ['$daily.present_count', 0]},
                    0
                ]
            },
            'late_count': {
                '$ifNull': [
                    {'$arrayElemAt': ['$daily.late_count', 0]},
                    0
                ]
            },
            'is_working_day': {
                '$and': [
                    {'$gte': [{'$dayOfWeek': '$date_obj'}, 2]},
                    {'$lte': [{'$dayOfWeek': '$date_obj'}, 6]}
                ]
            }
        }},
        {'$set': {
            'attendance_rate': {
                '$cond': [
                    {'$and': [
                        '$is_working_day',
                        {'$gt': ['$headcount', 0]}
                    ]},
                    {'$round': [
                        {'$divide': ['$present_count', '$headcount']},
                        4
                    ]},
                    None
                ]
            }
        }},
        {'$setWindowFields': {
            'sortBy': {'date_obj': 1},
            'output': {
                'moving_avg_7d': {
                    '$avg': '$attendance_rate',
                    'window': {'documents': [-6, 0]}
                }
            }
        }},
        {'$project': {
            '_id': 0,
            'date': '$date',
            'is_working_day': 1,
            'headcount': 1,
            'present_count': {'$round': ['$present_count', 2]},
            'late_count': 1,
            'attendance_rate': 1,
            'moving_avg_7d': {
                '$round': ['$moving_avg_7d', 4]
            }
        }},
        {'$sort': {'date': 1}}
    ]

    items = list(db.aggregate(pipeline))
    return {'department': department, 'items': items}


# Explain plans use the same filters and operations as each endpoint's primary query.


@app.get('/admin/explain/{endpoint}')
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_: Optional[str] = Query(None, alias='from'),
    to: Optional[str] = None
):
    if endpoint == 'attendance_list':
        q = {}
        if emp_code:
            q['emp_code'] = emp_code
        if status:
            q['status'] = status
        if date_from:
            q.setdefault('date', {})['$gte'] = date_from
        if date_to:
            q.setdefault('date', {})['$lte'] = date_to

        command = {
            'find': 'attendance_logs',
            'filter': q,
            'sort': {'date': -1, 'emp_code': 1},
            'skip': (page - 1) * page_size,
            'limit': page_size
        }
        coll = 'attendance_logs'

    elif endpoint == 'employee_monthly':
        if not emp_code or not month:
            bad('emp_code and month are required')

        start, end, _ = month_dates(month)

        pipe = [
            {
                '$match': {
                    'emp_code': emp_code,
                    'date': {'$gte': start, '$lt': end}
                }
            },
            {
                '$group': {
                    '_id': None,
                    'present_days': {
                        '$sum': {
                            '$cond': [
                                {'$in': ['$status', list(PRESENCE)]},
                                {
                                    '$cond': [
                                        {
                                            '$lt': [
                                                {'$ifNull': ['$work_hours', 999]},
                                                4.5
                                            ]
                                        },
                                        0.5, 1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    'leave_days': {
                        '$sum': {
                            '$cond': [
                                {'$eq': ['$status', 'LEAVE']}, 1, 0
                            ]
                        }
                    },
                    'late_count': {
                        '$sum': {
                            '$cond': [
                                {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]},
                                1, 0
                            ]
                        }
                    },
                    'total_late_minutes': {
                        '$sum': {'$ifNull': ['$late_minutes', 0]}
                    },
                    'total_overtime_minutes': {
                        '$sum': {'$ifNull': ['$overtime_minutes', 0]}
                    }
                }
            }
        ]

        command = {
            'aggregate': 'attendance_logs',
            'pipeline': pipe,
            'cursor': {}
        }
        coll = 'attendance_logs'

    elif endpoint == 'department_summary':
        if not month:
            bad('month is required')

        start, end, _ = month_dates(month)

        # Same attendance pipeline used by department_summary().
        attendance_pipeline = [
            {
                '$match': {
                    'date': {'$gte': start, '$lt': end}
                }
            },
            {
                '$lookup': {
                    'from': 'employees',
                    'localField': 'emp_code',
                    'foreignField': 'emp_code',
                    'as': 'emp'
                }
            },
            {'$unwind': '$emp'}
        ]

        if department is not None:
            attendance_pipeline.append({
                '$match': {'emp.department': department}
            })

        attendance_pipeline.append({
            '$group': {
                '_id': '$emp.department',
                'present_days': {
                    '$sum': {
                        '$cond': [
                            {
                                '$and': [
                                    {'$in': ['$status', list(PRESENCE)]},
                                    {
                                        '$gte': [
                                            {
                                                '$dayOfWeek': {
                                                    '$dateFromString': {
                                                        'dateString': '$date'
                                                    }
                                                }
                                            },
                                            2
                                        ]
                                    },
                                    {
                                        '$lte': [
                                            {
                                                '$dayOfWeek': {
                                                    '$dateFromString': {
                                                        'dateString': '$date'
                                                    }
                                                }
                                            },
                                            6
                                        ]
                                    }
                                ]
                            },
                            {
                                '$cond': [
                                    {
                                        '$lt': [
                                            {'$ifNull': ['$work_hours', 999]},
                                            4.5
                                        ]
                                    },
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                'hours': {
                    '$avg': {
                        '$cond': [
                            {
                                '$and': [
                                    {'$in': ['$status', list(PRESENCE)]},
                                    {'$ne': ['$work_hours', None]}
                                ]
                            },
                            '$work_hours',
                            None
                        ]
                    }
                },
                'late_count': {
                    '$sum': {
                        '$cond': [
                            {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                'total_late_minutes': {
                    '$sum': {'$ifNull': ['$late_minutes', 0]}
                },
                'leave_count': {
                    '$sum': {
                        '$cond': [
                            {'$eq': ['$status', 'LEAVE']},
                            1,
                            0
                        ]
                    }
                },
                'on_duty_count': {
                    '$sum': {
                        '$cond': [
                            {'$eq': ['$status', 'ON_DUTY']},
                            1,
                            0
                        ]
                    }
                }
            }
        })

        employee_match = {'joined_on': {'$lt': end}}
        if department is not None:
            employee_match['department'] = department

        # Same headcount pipeline used by department_summary().
        headcount_pipeline = [
            {'$match': employee_match},
            {
                '$group': {
                    '_id': '$department',
                    'headcount': {'$sum': 1}
                }
            }
        ]

        attendance_explain = db.command(
            'explain',
            {
                'aggregate': 'attendance_logs',
                'pipeline': attendance_pipeline,
                'cursor': {}
            },
            verbosity='executionStats'
        )

        headcount_explain = db.command(
            'explain',
            {
                'aggregate': 'employees',
                'pipeline': headcount_pipeline,
                'cursor': {}
            },
            verbosity='executionStats'
        )

        return {
            'endpoint': endpoint,
            'collection': 'attendance_logs and employees',
            'explain': {
                'attendance_metrics': attendance_explain,
                'department_headcount': headcount_explain
            }
        }

    elif endpoint == 'late_leaderboard':
        if not month:
            bad('month is required')

        start, end, _ = month_dates(month)

        match = {
            'date': {'$gte': start, '$lt': end},
            'late_minutes': {'$gt': 0}
        }

        pipe = [
            {'$match': match},
            {
                '$group': {
                    '_id': '$emp_code',
                    'total_late_minutes': {'$sum': '$late_minutes'},
                    'late_count': {'$sum': 1}
                }
            },
            {
                '$lookup': {
                    'from': 'employees',
                    'localField': '_id',
                    'foreignField': 'emp_code',
                    'as': 'emp'
                }
            },
            {'$unwind': '$emp'}
        ]

        if department is not None:
            pipe.append({'$match': {'emp.department': department}})

        pipe += [
            {'$sort': {'total_late_minutes': -1, '_id': 1}},
            {
                '$setWindowFields': {
                    'sortBy': {'total_late_minutes': -1},
                    'output': {'rank': {'$rank': {}}}
                }
            },
            {'$match': {'rank': {'$lte': limit}}},
            {
                '$project': {
                    '_id': 0,
                    'rank': 1,
                    'emp_code': '$_id',
                    'name': '$emp.name',
                    'department': '$emp.department',
                    'total_late_minutes': 1,
                    'late_count': 1
                }
            }
        ]

        command = {
            'aggregate': 'attendance_logs',
            'pipeline': pipe,
            'cursor': {}
        }
        coll = 'attendance_logs'

    elif endpoint == 'department_trend':
        if not department or not from_ or not to:
            bad('department, from and to are required')

        a = valid_date(from_)
        b = valid_date(to)

        if b < a or (b - a).days + 1 > 92:
            bad('date range must be ordered and no longer than 92 days')

        pipe = [
            {'$match': {'date': {'$gte': from_, '$lte': to}}},
            {
                '$lookup': {
                    'from': 'employees',
                    'localField': 'emp_code',
                    'foreignField': 'emp_code',
                    'as': 'emp'
                }
            },
            {'$unwind': '$emp'},
            {'$match': {'emp.department': department}},
            {
                '$group': {
                    '_id': '$date',
                    'present_count': {
                        '$sum': {
                            '$cond': [
                                {'$in': ['$status', list(PRESENCE)]},
                                {
                                    '$cond': [
                                        {
                                            '$lt': [
                                                {'$ifNull': ['$work_hours', 999]},
                                                4.5
                                            ]
                                        },
                                        0.5, 1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    'late_count': {
                        '$sum': {
                            '$cond': [
                                {'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]},
                                1, 0
                            ]
                        }
                    }
                }
            },
            {
                '$project': {
                    '_id': 0,
                    'date': {
                        '$dateFromString': {'dateString': '$_id'}
                    },
                    'present_count': 1,
                    'late_count': 1
                }
            },
            {
                '$densify': {
                    'field': 'date',
                    'range': {
                        'step': 1,
                        'unit': 'day',
                        'bounds': [
                            datetime.combine(a, time.min),
                            datetime.combine(b, time.min)
                        ]
                    }
                }
            },
            {
                '$set': {
                    'present_count': {'$ifNull': ['$present_count', 0]},
                    'late_count': {'$ifNull': ['$late_count', 0]}
                }
            },
            {
                '$lookup': {
                    'from': 'employees',
                    'let': {'ds': '$date'},
                    'pipeline': [
                        {
                            '$match': {
                                '$expr': {
                                    '$and': [
                                        {'$eq': ['$department', department]},
                                        {
                                            '$lte': [
                                                '$joined_on',
                                                {
                                                    '$dateToString': {
                                                        'date': '$$ds',
                                                        'format': '%Y-%m-%d',
                                                        'timezone': 'UTC'
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        {'$count': 'n'}
                    ],
                    'as': 'headcount_result'
                }
            },
            {
                '$set': {
                    'headcount': {
                        '$ifNull': [
                            {'$arrayElemAt': ['$headcount_result.n', 0]},
                            0
                        ]
                    },
                    'is_working_day': {
                        '$and': [
                            {'$gte': [{'$dayOfWeek': '$date'}, 2]},
                            {'$lte': [{'$dayOfWeek': '$date'}, 6]}
                        ]
                    }
                }
            },
            {
                '$set': {
                    'attendance_rate': {
                        '$cond': [
                            {
                                '$and': [
                                    '$is_working_day',
                                    {'$gt': ['$headcount', 0]}
                                ]
                            },
                            {
                                '$round': [
                                    {'$divide': ['$present_count', '$headcount']},
                                    4
                                ]
                            },
                            None
                        ]
                    }
                }
            },
            {
                '$setWindowFields': {
                    'sortBy': {'date': 1},
                    'output': {
                        'moving_avg_7d': {
                            '$avg': '$attendance_rate',
                            'window': {'documents': [-6, 0]}
                        }
                    }
                }
            },
            {
                '$project': {
                    '_id': 0,
                    'date': {
                        '$dateToString': {
                            'date': '$date',
                            'format': '%Y-%m-%d',
                            'timezone': 'UTC'
                        }
                    },
                    'is_working_day': 1,
                    'headcount': 1,
                    'present_count': {'$round': ['$present_count', 2]},
                    'late_count': 1,
                    'attendance_rate': 1,
                    'moving_avg_7d': {'$round': ['$moving_avg_7d', 4]}
                }
            },
            {'$sort': {'date': 1}}
        ]

        command = {
            'aggregate': 'attendance_logs',
            'pipeline': pipe,
            'cursor': {}
        }
        coll = 'attendance_logs'

    else:
        bad('unknown endpoint')

    exp = db.command(
        'explain',
        command,
        verbosity='executionStats'
    )

    return {
        'endpoint': endpoint,
        'collection': coll,
        'explain': exp
    }


