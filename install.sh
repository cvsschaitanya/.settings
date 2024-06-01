#!/bin/bash

getTarget() {
    header=`grep "__#__.*__#__" $1 | head -1 | awk 'BEGIN{FS="__#__"}{print $2}'`
    eval echo "$header"
}

for set in `ls theSettings/*`
do
    target=`getTarget $set`
    
    if [[ -e $target ]]; then
        mv $target /tmp/`basename $target`.`date +%s`
    fi
    
    ln -s `realpath $set` $target
done

