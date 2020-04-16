#!/usr/bin/env python3

import sys
import re
import bisect
import sqlite3
import argparse
from sys import argv
import numpy as np
import pysam


def sqldb_creation(contigs, mags, sample_reads, map_format, database):
    """ Read information provided by user and creates SQLite3 database
    
    Arguments:
        contigs {str} -- Location of fasta file with contigs of interest.
        mags {str} -- Location of tab separated file with contigs and their corresponding mags.
        sample_reads {list} -- Location of one or more read mapping results file(s).
        format {str} -- Format of read mapping (blast or sam).
        database {str} -- Name (location) of database to create.
    """
    
    # ===== Database and table creation =====
    # Create or open database
    print("Creating database...")
    conn = sqlite3.connect(database)
    cursor = conn.cursor()
    # Create lookup table (always creates a new one)
    cursor.execute('DROP TABLE IF EXISTS lookup_table')
    cursor.execute('CREATE TABLE lookup_table \
        (mag_name TEXT, mag_id INTEGER, contig_name TEXT, contig_id INTEGER)')
    # Create sample_info, mag_info, mags_per_sample, and gene_info tables
    cursor.execute('DROP TABLE IF EXISTS mag_info')
    cursor.execute('DROP TABLE IF EXISTS gene_info')
    cursor.execute('DROP TABLE IF EXISTS sample_info')
    cursor.execute('DROP TABLE IF EXISTS mags_per_sample')
    cursor.execute('CREATE TABLE mag_info \
        (mag_id INTEGER, contig_id INTEGER, contig_len INTEGER)')
    cursor.execute('CREATE TABLE gene_info \
        (mag_id INTEGER, contig_id INTEGER, gene TEXT, gene_start INTEGER, gene_stop INTEGER)')
    cursor.execute('CREATE TABLE sample_info \
        (sample_name TEXT, sample_id TEXT, sample_number INTEGER)')
    cursor.execute('CREATE TABLE mags_per_sample \
        (sample_name TEXT, mag_name TEXT)')
    # ========

    # === Extract sample information and save in into DB ===
    # Rename samples provided to avoid illegal names on files
    sampleid_to_sample = {}
    samples_to_db = []
    sample_number = 1
    for sample_name in sample_reads:
        sample_id = "sample_" + str(sample_number)
        samples_to_db.append((sample_name, sample_id, sample_number))
        sampleid_to_sample[sample_id] = sample_name
        sample_number += 1
    # Enter information into table
    cursor.execute("begin")
    cursor.executemany('INSERT INTO sample_info VALUES(?, ?, ?)', samples_to_db)
    cursor.execute('CREATE UNIQUE INDEX sample_index ON sample_info (sample_name)')
    cursor.execute("commit")
    # ========

    # === Extract contig information and MAG correspondence. Save into DB. ===
    
    # Get contig sizes
    contig_sizes = read_contigs(contigs)
    
    # Get contig - MAG information
    contig_mag_corresp = get_mags(mags)
    
    # Initialize variables
    contig_identifiers = []
    mag_ids = {}
    mag_id = 0
    contig_id = 1
    # The dictionary contig_information is important for speed when filling tables
    contig_information = {}
    # Iterate through contig - MAG pairs
    for contig_name, mag_name in contig_mag_corresp.items():
        # Store MAG (and contig) names and ids
        if mag_name in mag_ids:
            contig_identifiers.append((mag_name, mag_ids[mag_name], contig_name, contig_id))
            contig_information[contig_name] = [mag_name, mag_ids[mag_name], contig_id]
            contig_id += 1
        else:
            mag_id += 1
            mag_ids[mag_name] = mag_id
            contig_identifiers.append((mag_name, mag_ids[mag_name], contig_name, contig_id))
            contig_information[contig_name] = [mag_name, mag_ids[mag_name], contig_id]
            contig_id += 1
    cursor.executemany('INSERT INTO lookup_table VALUES(?, ?, ?, ?)', contig_identifiers)
    cursor.execute('CREATE INDEX mag_name_index ON lookup_table (mag_name)')
    conn.commit()
    # ========

    # === Fill contig length table ===
    contig_lengths = []
    for contig, contig_len in contig_sizes.items():
        # Get mag_id and contig_id
        sql_command = 'SELECT mag_id, contig_id from lookup_table WHERE contig_name = ?'
        cursor.execute(sql_command, (contig,))
        mag_contig_id = cursor.fetchone()        
        contig_lengths.append((mag_contig_id[0], mag_contig_id[1], contig_len))
    
    
    cursor.executemany('INSERT INTO mag_info VALUES(?, ?, ?)', contig_lengths)
    cursor.execute('CREATE INDEX mag_id_index ON mag_info (mag_id)')
    conn.commit()
    # ========

    # === Create one table with information per sample ===
    for sample_name in sampleid_to_sample.keys():
        # Drop if they exist
        cursor.execute('DROP TABLE IF EXISTS ' + sample_name)
        # Create tables once again
        cursor.execute('CREATE TABLE ' + sample_name + \
            ' (mag_id INTEGER, contig_id INTEGER, identity FLOAT, start INTEGER, stop INTEGER)')
    # === Retrieve information from read mapping and store it ===
    # Read read mapping file for each sample and fill corresponding table
    for sample_name, mapping_file in sampleid_to_sample.items():
        mags_in_sample = []
        print("Parsing {}... ".format(mapping_file))
        contigs_in_sample = save_reads_mapped(mapping_file, sample_name, map_format, contig_mag_corresp, cursor, conn)
        cursor.execute('SELECT contig_name, mag_name, mag_id FROM lookup_table')
        all_contigs = cursor.fetchall()
        for element in all_contigs:
            if element[0] in contigs_in_sample:
                if element[1] not in mags_in_sample:
                    mags_in_sample.append(element[1])
                else:
                    continue
            else:
                continue
        mags_in_sample = [(mapping_file, x) for x in mags_in_sample]
        cursor.executemany('INSERT INTO mags_per_sample VALUES(?, ?)', mags_in_sample)
        conn.commit()
        print("Done")
    conn.commit()
    conn.close()
    # ========

def save_reads_mapped(mapping_file, sample_name, map_format, contig_mag_corresp, cursor, conn):
    """ This script reads a read mapping file, extracts the contig to which each read maps,
        the percent id, the start and stop, and stores it in a table per sample.
    
    Arguments:
        mapping_file {str} -- Location of read mapping file.
        sample_name {str} -- Name of database sample name (form sample_#)
        map_format {str} -- Format of read mapping results (blast or sam)
        contig_mag_corresp {dict} -- Dictionary with contigs (keys) and mags (values)
        cursor {obj} -- Cursor to execute db instructions
        conn {obj} -- Connection handler to db.
    """
    assert (map_format == "sam" or map_format == "bam" or map_format == "blast"), "Mapping format not recognized. Must be one of 'sam' 'bam' or 'blast'"
    
    contigs_in_sample = []
	
    if map_format == "blast":
        print("Parsing tabular BLAST format reads... ")
        with open(mapping_file) as input_reads:
            record_counter = 0
            records = []
            for line in input_reads:
                # Commit changes after 500000 records
                if record_counter == 500000:
                    cursor.execute("begin")
                    cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
                    cursor.execute("commit")
                    record_counter = 0
                    records = []
                if line.startswith("#"):
                    pass
                else:
                    segment = line.split("\t")
                    contig_ref = segment[1]
                    # Exclude reads not associated with MAGs of interest
                    if contig_ref not in contig_mag_corresp:
                        continue
                    else:
                        if contig_ref not in contigs_in_sample:
                            contigs_in_sample.append(contig_ref)
                        pct_id = float(segment[2])
                        pos1 = int(segment[8])
                        pos2 = int(segment[9])
                        start = min(pos1, pos2)
                        end = start+(max(pos1, pos2)-min(pos1, pos2))
                        mag_id = contig_mag_corresp[contig_ref][1]
                        contig_id = contig_mag_corresp[contig_ref][2]
                        records.append((mag_id, contig_id, pct_id, start, end))
                        record_counter += 1
            # Commit remaining records
            if record_counter > 0:
                cursor.execute("begin")
                cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
                cursor.execute("commit")
            # Create index for faster access
            cursor.execute('CREATE INDEX ' + sample_name + '_index on ' + sample_name + ' (mag_id)')
            
    if map_format == "sam":
        print("Parsing SAM format reads... ")
        record_counter = 0
        records = []
        with open(mapping_file) as input_reads:
            for line in input_reads:
                if record_counter == 500000:
                    cursor.execute("begin")
                    cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
                    cursor.execute("commit")
                    record_counter = 0
                    records = []
                if "MD:Z:" not in line:
                    continue
                else :
                    segment = line.split()
                    contig_ref = segment[2]
                    # Exclude reads not associated with MAGs of interest
                    if contig_ref not in contig_mag_corresp:
                        continue
                    else:
                        if contig_ref not in contigs_in_sample:
                            contigs_in_sample.append(contig_ref)
                        # Often the MD:Z: field will be the last one in a magicblast output, but not always.
                        # Therefore, start from the end and work in.
                        iter = len(segment)-1
                        mdz_seg = segment[iter]
                        # If it's not the correct field, proceed until it is.
                        while not mdz_seg.startswith("MD:Z:"):
                            iter -= 1
                            mdz_seg = segment[iter]
                        #Remove the MD:Z: flag from the start
                        mdz_seg = mdz_seg[5:]
                        match_count = re.findall('[0-9]+', mdz_seg)
                        sum=0
                        for num in match_count:
                            sum+=int(num)
                        total_count = len(''.join([i for i in mdz_seg if not i.isdigit()])) + sum
                        pct_id = (sum/(total_count))*100
                        start = int(segment[3])
                        end = start+total_count-1
                        # Get mag_id and contig_id
                        mag_id = contig_mag_corresp[contig_ref][1]
                        contig_id = contig_mag_corresp[contig_ref][2]
                        records.append((mag_id, contig_id, pct_id, start, end))
                        record_counter += 1
            # Commit remaining records
            if record_counter > 0:
                cursor.execute("begin")
                cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
                cursor.execute("commit")
            # Create index for faster access
            cursor.execute('CREATE INDEX ' + sample_name + '_index on ' + sample_name + ' (mag_id)')
	
    if map_format == "bam":
        print("Parsing BAM format reads... ")
        record_counter = 0
        records = []
        input_reads = pysam.AlignmentFile(mapping_file, "rb")
        for entry in input_reads:
            line = entry.to_string()
            if record_counter == 500000:
                cursor.execute("begin")
                cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
                cursor.execute("commit")
                record_counter = 0
                records = []
            if "MD:Z:" not in line:
                continue
            else :
                segment = line.split()
				
                contig_ref = segment[2]
				
                print(contig_ref)
								
                # Exclude reads not associated with MAGs of interest
                if contig_ref not in contig_mag_corresp:
                    continue
                else:
                    if contig_ref not in contigs_in_sample:
                        contigs_in_sample.append(contig_ref)
                    
                    mdz_seg = entry.get_tag("MD")
                    match_count = re.findall('[0-9]+', mdz_seg)
                    sum=0
                    for num in match_count:
                        sum+=int(num)
                    total_count = len(''.join([i for i in mdz_seg if not i.isdigit()])) + sum
                    pct_id = (sum/(total_count))*100
                    start = int(segment[3])
                    end = start+total_count-1
                    # Get mag_id and contig_id
                    mag_id = contig_mag_corresp[contig_ref][1]
                    contig_id = contig_mag_corresp[contig_ref][2]
					
                    #print(mag_id, contig_id)
					
                    records.append((mag_id, contig_id, pct_id, start, end))
					
                    #print(*records)
					
                    record_counter += 1
        # Commit remaining records
        if record_counter > 0:
            cursor.execute("begin")
            cursor.executemany('INSERT INTO ' + sample_name + ' VALUES(?, ?, ?, ?, ?)', records)
            cursor.execute("commit")
        # Create index for faster access
        cursor.execute('CREATE INDEX ' + sample_name + '_index on ' + sample_name + ' (mag_id)')
	
    conn.commit()
    
    return contigs_in_sample


def add_sample(database, new_mapping_files, map_format):
    contig_mag_corresp = {}
    samples_dict = {}
    last_sample = 0
    conn = sqlite3.connect(database)
    cursor = conn.cursor()
    # Retrieve all sample information
    sql_command = 'SELECT * from sample_info'
    cursor.execute(sql_command)
    sample_information = cursor.fetchall()
    for sample in sample_information:
        samples_dict[sample[0]] = sample[1]
        if sample[2] > last_sample:
            last_sample = sample[2]
    # Retrieve contig - mag correspondence
    sql_command = 'SELECT * from lookup_table'
    cursor.execute(sql_command)
    contig_correspondence = cursor.fetchall()
    for contig_mag in contig_correspondence:
        contig_mag_corresp[contig_mag[2]] = [contig_mag[0], contig_mag[1], contig_mag[3]]
    for new_sample in new_mapping_files:
        # Check if new sample exists
        mags_in_sample = []
        if new_sample in samples_dict:
            print("Dropping {} table and re-building it".format(new_sample))
            sample_name = samples_dict[new_sample]
            # If it does, drop the reads that table from that sample and re-build it
            cursor.execute('DROP TABLE IF EXISTS ' + sample_name)
            cursor.execute('CREATE TABLE ' + sample_name + \
                ' (mag_id INTEGER, contig_id INTEGER, identity FLOAT, start INTEGER, stop INTEGER)')
            cursor.execute('DELETE FROM mags_per_sample WHERE sample_name = ?', (new_sample,))
            conn.commit()
            print("Adding {}... ".format(new_sample))
            contigs_in_sample = save_reads_mapped(new_sample, sample_name, map_format, contig_mag_corresp, cursor, conn)
            cursor.execute('SELECT contig_name, mag_name, mag_id FROM lookup_table')
            all_contigs = cursor.fetchall()
            for element in all_contigs:
                if element[0] in contigs_in_sample:
                    if element[1] not in mags_in_sample:
                        mags_in_sample.append(element[1])
                    else:
                        continue
                else:
                    continue
            mags_in_sample = [(new_sample, x) for x in mags_in_sample]
            cursor.execute("begin")
            cursor.executemany('INSERT INTO mags_per_sample VALUES(?, ?)', mags_in_sample)
            cursor.execute("commit")

        else:
            # Otherwise create the new table and add the read information
            print("Adding {} to existing database {}... ".format(new_sample, database))
            sample_name = "sample_" + str(last_sample + 1)
            last_sample += 1
            cursor.execute('CREATE TABLE ' + sample_name + \
                ' (mag_id INTEGER, contig_id INTEGER, identity FLOAT, start INTEGER, stop INTEGER)')
            contigs_in_sample = save_reads_mapped(new_sample, sample_name, map_format, contig_mag_corresp, cursor, conn)
            cursor.execute('SELECT contig_name, mag_name, mag_id FROM lookup_table')
            all_contigs = cursor.fetchall()
            for element in all_contigs:
                if element[0] in contigs_in_sample:
                    if element[1] not in mags_in_sample:
                        mags_in_sample.append(element[1])
                    else:
                        continue
                else:
                    continue
            mags_in_sample = [(new_sample, x) for x in mags_in_sample]
            cursor.execute("begin")
            cursor.executemany('INSERT INTO mags_per_sample VALUES(?, ?)', mags_in_sample)
            cursor.execute("commit")
            # Add to sample_info table
            new_record = (new_sample, sample_name, last_sample)
            cursor.execute('INSERT INTO sample_info VALUES(?, ?, ?)', new_record)
            conn.commit()
        print("Done")
        conn.commit()
    conn.close()


def read_contigs(contig_file_name):
    """ Reads a FastA file and returns
        sequence ids and sizes
    
    Arguments:
        contig_file_name {[str]} -- FastA file location
    Returns:
        [dict] -- Dictionary with ids and sizes
    """
    print("Reading contigs... ", end="", flush=True)
    
    contig_sizes = {}
    contig_length = 0
    contigs =  open(contig_file_name, 'r')
    
    #The ensuing loop commits a contig to the contig lengths dict every time a new contig is observed, i.e. whenever a current sequence has terminated.
    #This works both for single and splitline multi fastas.
    
    #The first line is manually read in so that its count can be gathered before it is committed - this basically skips the first iteration of the loop.
    current_contig = contigs.readline()[1:].strip().split()[0]
    
    for line in contigs:
        if line[0] == ">":
            #Add the contig that had 
            contig_sizes[current_contig] = contig_length
            
            #set to new contig. One final loop of starts ends counts is needed
            current_contig = line[1:].strip().split()[0]
            contig_length = 0
        else :
            contig_length += len(line.strip())
    
    contigs.close()
    
    #The loop never gets to commit on the final iteration, so this statement adds the last contig.
    contig_sizes[current_contig] = contig_length
    
    print("done!")
    
    return contig_sizes


def get_mags(mag_file):
    """ Reads a file with columns:
        Contig_name MAG_name
        and returns the corresponding MAG per contig
    
    Arguments:
        mag_file {[str]} -- MAG correspondence file location
    Returns:
        [dict] -- Dictionary with contigs and corresponding MAG
    """
    mag_dict = {}
    with open(mag_file, 'r') as mags:
        for line in mags:
            mag_contig = line.split()
            mag_dict[mag_contig[0]] = mag_contig[1]
    return mag_dict

#The purpose of this function is to prepare an empty recplot matrix from a set of contig names and lengths associated with one MAG
#However, Numpy is slower than base python for this purpose, so this function should be unused.
def prepare_numpy_matrices(database, mag_name, width, bin_height, id_lower):
    """ Extracts information of a requested mag and builds the empty matrices
        to be filled in the next step.
    
    Arguments:
        database {str} -- Name of database to use (location).
        mag_name {str} -- Name of mag of interest.
        width {int} -- Width of the "bins" to split contigs into.
        bin_height {list} -- List of identity percentages to include.
        id_lower {int} -- Minimum identity percentage to consider for reads mapped.
    
    Returns:
        mag_id [int] -- ID of the mag in the database.
        matrix [dict] -- Dictionary with list of arrays of start and stop positions
                         and empty matrix to fill.
        id_breaks [list] -- List of identity percentages to include.
    """
    print("Preparing recruitment matrices...", end="", flush=True)
    # Prep percent identity breaks - always starts at 100 and proceeds 
    # down by bin_height steps until it cannot do so again without passing id_lower
    id_breaks = []
    current_break = 100
    while current_break > id_lower:
        id_breaks.append(current_break)
        current_break -= bin_height
    id_breaks = np.array(id_breaks[::-1])
    

    # Retrieve mag_id from provided mag_name
    conn = sqlite3.connect(database)
    cursor = conn.cursor()
    sql_command = 'SELECT mag_id from lookup_table WHERE mag_name = ?'
    cursor.execute(sql_command, (mag_name,))
    mag_id = cursor.fetchone()[0]
    # Retrieve all contigs from mag_name and their sizes
    sql_command = 'SELECT contig_id, contig_len from mag_info WHERE mag_id = ?'
    cursor.execute(sql_command, (mag_id,))
    contig_sizes = cursor.fetchall()
    # Create matrices for each contig in the mag_name provided
    matrix = {}
    for id_len in contig_sizes:
        contig_length = id_len[1]
        num_bins = int(contig_length / width)
        if num_bins < 1:
            num_bins = 1
        starts = np.linspace(1, contig_length, num = num_bins, dtype = np.uint64, endpoint = False)
        ends = np.append(starts[1:]-1, contig_length)
        numpy_arr = np.zeros((len(id_breaks), len(starts)), dtype = np.uint32)
        matrix[id_len[0]] = [starts, ends, numpy_arr]
    print("Done")
    return mag_id, matrix, id_breaks

#The purpose of this function is to take an empty recplot matrix object associated with one MAG, query the database for the sample and MAG in question,
#And fill the database with the returned information.
def fill_matrices(database, mag_id, sample_name, matrices, id_breaks):
    """[summary]
    
    Arguments:
        database {str} -- Name of database to use (location).
        mag_id {int} -- ID of mag of interest.
        sample_name {str} -- Name of sample to retrieve reads from.
        matrices {dict} -- Empty matrices to fill.
        id_breaks {list} -- List of identity percentages to include.
    
    Returns:
        matrix [dict] -- Dictionary with list of arrays of start and stop positions
                         and filled matrix to plot.
    """
    print("Filling matrices...")
    # Retrieve sample_id from sample_name provided
    conn = sqlite3.connect(database)
    cursor = conn.cursor()
    sql_command = 'SELECT sample_id from sample_info WHERE sample_name = ?'
    cursor.execute(sql_command, (sample_name,))
    sample_id = cursor.fetchone()[0]
    # Retrieve all read information from mag_name and sample_name provided
    sql_command = 'SELECT * from ' + sample_id + ' WHERE mag_id = ?'
    cursor.execute(sql_command, (mag_id,))
    
    #TODO: We shouldn't need to fetch all reads. We can iterate on the cursor without doing this.
    #read_information = cursor.fetchall()
    #read_information is (mag_id contig_id perc_id read_start read_stop)
    #for read_mapped in read_information:
    for read_mapped in cursor:
        if read_mapped[1] in matrices:
            contig_id = read_mapped[1]
            read_start = read_mapped[3]
            read_stop = read_mapped[4]
            read_len = read_stop - read_start + 1
            read_id_index = bisect.bisect_right(id_breaks, read_mapped[2]) - 1
            # print(read_mapped)
            # print(id_breaks)
            # print(read_id_index, id_breaks[read_id_index])
            read_start_loc = bisect.bisect_left(matrices[contig_id][1], read_start)
            read_stop_loc = bisect.bisect_left(matrices[contig_id][1], read_stop)
            # If the read falls entirely on a bin add all bases to the bin
            if read_start_loc == read_stop_loc:
                matrices[contig_id][2][read_id_index][read_start_loc] += read_len
            # On the contrary split bases between two or more bins
            else:
                for j in range(read_start_loc, read_stop_loc + 1):
                    overflow = read_stop - matrices[contig_id][1][j]
                    if overflow > 0:
                        matrices[contig_id][2][read_id_index][j] += (read_len - overflow)
                        read_len = overflow
                    else :
                        matrices[contig_id][2][read_id_index][j] += read_len
    print("Done")
    return matrices

#The purpose of this function is to prepare an empty recplot matrix from a set of contig names and lengths associated with one MAG
def prepare_matrices(database, mag_name, width, bin_height, id_lower):
    #Prep percent identity breaks - always starts at 100 and proceeds down by bin_height steps until it cannot do so again without passing id_lower
    print("Preparing recruitment matrices...", end="", flush=True)
    # Prep percent identity breaks - always starts at 100 and proceeds 
    # down by bin_height steps until it cannot do so again without passing id_lower
    id_breaks = []
    current_break = 100
    while current_break > id_lower:
        id_breaks.append(current_break)
        current_break -= bin_height
    id_breaks = np.array(id_breaks[::-1])
    
    zeroes = []
    for i in id_breaks:
        zeroes.append(0)
    
    # Retrieve mag_id from provided mag_name
    conn = sqlite3.connect(database)
    cursor = conn.cursor()
    sql_command = 'SELECT mag_id from lookup_table WHERE mag_name = ?'
    cursor.execute(sql_command, (mag_name,))
    mag_id = cursor.fetchone()[0]
    # Retrieve all contigs from mag_name and their sizes
    sql_command = 'SELECT contig_id, contig_len from mag_info WHERE mag_id = ?'
    cursor.execute(sql_command, (mag_id,))
    contig_sizes = cursor.fetchall()
    # Create matrices for each contig in the mag_name provided
    matrix = {}
    #begin reading contigs and determining their lengths.
    
    #id_len is a list of contig name, contig_length
    for id_len in contig_sizes:
        
        starts = []
        ends = []
        pct_id_counts = []
        
        contig_length = id_len[1]
        
        num_bins = int(contig_length / width)
        if num_bins < 1:
            num_bins = 1
        
        bin_width = (contig_length / num_bins)-1

        cur_bin_start = 1
        
        for i in range(1, num_bins):
            starts.append(int(cur_bin_start))
            ends.append(int((cur_bin_start+bin_width)))
            pct_id_counts.append(zeroes[:])
            cur_bin_start+=bin_width+1
        
        matrix[id_len[0]] = [starts, ends, numpy_arr]

        
    return(mag_id, matrices, id_breaks)


#A function for reading args
def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
            description='''This script builds recruitment plots (COMPLETE DESCRIPTION HERE)\n'''
            '''Usage: ''' + argv[0] + ''' COMPLETE\n'''
            '''Global mandatory parameters: -g [Genome Files] OR -p [Protein Files] OR -s [SCG HMM Results] -o [AAI Table Output]\n'''
            '''Optional Database Parameters: See ''' + argv[0] + ' -h')

    parser.add_argument("-c", "--contigs", dest="contigs", 
    help = "This should be a FASTA file containing all and only the contigs that you would like to be part of your recruitment plot.")
    parser.add_argument("-m", "--mags", dest="mags", default="", help = "A tab separated file containing the names of MAGs in the first column and contigs in the second column. Every contig should have its parent MAG listed in this file.")    
    parser.add_argument("-r", "--reads", dest="reads", nargs='+', help = "This should be a file with reads aligned to your contigs in any of the following formats: tabular BLAST(outfmt 6), SAM, or Magic-BLAST")
    parser.add_argument("-f", "--format", dest="map_format", default="blast", help="The format of the reads file (write 'blast' or 'sam'). Defaults to tabular BLAST.")
    #parser.add_argument("-g", "--genes", dest="genes", default = "", help = "Optional GFF3 file containing gene starts and stops to be use in the recruitment plot.")
    parser.add_argument("-i", "--ID-step", dest="id_step", default = 0.5, help = "Percent identity bin width. Default 0.5.")
    parser.add_argument("-w", "--bin-width", dest="bin_width", default = 1000, help = "Approximate genome bin width in bp. Default 1000.")
    parser.add_argument("-o", "--output", dest="out_file_name", default = "recruitment_plot", help = "Prefix of results to be output. Default: 'recruitment_plot'")
    parser.add_argument("-e", "--export", dest="output_line", action='store_true', help = "Output sam lines to stdout?")
    parser.add_argument("-d", "--database", dest="sql_database", action='store', help = "SQLite database to create or update")
    parser.add_argument("-s", "--stats", dest="stats", action='store_true', help = "Write ANIr prep file?")
    parser.add_argument("--interact", dest="lim_rec", action='store_true', help = "Create lim/rec files for each MAG; do NOT make recruitment matrices.")
    
    args = parser.parse_args()
    
   
    contigs = args.contigs
    reads = args.reads
    mags = args.mags
    map_format = args.map_format
    # genes = args.genes
    step = float(args.id_step)
    width = int(args.bin_width)
    prefix = args.out_file_name
    export_lines = args.output_line
    do_stats = args.stats
    interact = args.lim_rec
    sql_database = args.sql_database
    
    # Create databases
    sqldb_creation(contigs, mags, reads, map_format, sql_database)

    # Prepare user requested information
    mag_id, matrix, id_breaks = prepare_matrices("TEST_DB", "IIa.A_ENTP2013_S02_SV82_300m_MAG_01", width, step, 70)
    matrix = fill_matrices(sql_database, mag_id, "03.All_SAR11--ETNP_2013_S02_SV89_300m.blast.bh", matrix, id_breaks)

    # Add new sample to database
    add_sample(sql_database, ["03.First_Mapping.blast.bh", "TEST"], map_format)


    mags = {}
    
   

#Just runs main.
#if __name__ == "__main__":main()
