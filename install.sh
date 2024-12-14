#!/bin/bash

getTarget() {
    header=`grep "__#__.*__#__" $1 | head -1 | awk 'BEGIN{FS="__#__"}{print $2}'`
    eval echo "$header"
}


#!/bin/bash

# Get the operating system
os_type=$(uname)

if [[ "$os_type" == "Darwin" ]]; then
    echo "This is macOS."
    sourcePath="theSettings/macos"
elif [[ "$os_type" == "Linux" ]]; then
    echo "This is Linux."
    sourcePath="theSettings/linux"
else
    echo "Unknown OS."
fi

for set in `ls $sourcePath/*`
do
    target=`getTarget $set`
    
    if [[ -e $target ]]; then
        mv $target /tmp/`basename $target`.`date +%s`
    fi
    
    echo cp `realpath $set` $target
    cp `realpath $set` $target
done

