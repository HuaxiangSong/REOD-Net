import sys,os

def get_time_prefix(option:str):
    import time
    index = 3
    output_str = ''
    if option == 'date':
        index =3
    if option == 'second':
        index =6
    for number in time.localtime()[:index]:
        output_str += str(number).zfill(2)
        
    return output_str
#--------------------------------------------------------