'''
This module houses various functions and classes that Luigi uses to set up calculations that
can be submitted to Fireworks. This is intended to be used in conjunction with a submission
file, an example of which is named "adsorbtionTargets.py".
'''
from collections import OrderedDict
import copy
import math
from math import ceil
import cPickle as pickle
import numpy as np
from numpy.linalg import norm
from pymatgen.matproj.rest import MPRester
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.core.surface import SlabGenerator
from pymatgen.analysis.structure_analyzer import average_coordination_number
from ase.db import connect
from ase import Atoms
from ase.geometry import find_mic
from ase.build import rotate
from ase.calculators.singlepoint import SinglePointCalculator
from ase.collections import g2
from fireworks import Workflow
from vasp.mongo import mongo_doc, mongo_doc_atoms
import luigi


LOCAL_DB_PATH = '/global/cscratch1/sd/zulissi/GASpy_DB/'


class DumpBulkGasToAuxDB(luigi.Task):
    '''
    This class will load the results for bulk and slab relaxations from the Primary FireWorks
    database into the Auxiliary vasp.mongo database.
    '''

    def run(self):
        lpad = get_launchpad()

        # Create a class, "con", that has methods to interact with the database.
        with get_aux_db() as aux_db:

            # A list of integers containing the Fireworks job ID numbers that have been
            # added to the database already
            fws = [a['fwid'] for a in aux_db.find({'fwid':{'$exists':True}})]

            # Get all of the completed fireworks for unit cells and gases
            fws_cmpltd = lpad.get_fw_ids({'state':'COMPLETED',
                                          'name.calculation_type':'unit cell optimization'}) + \
                         lpad.get_fw_ids({'state':'COMPLETED',
                                          'name.calculation_type':'gas phase optimization'})

            # For each fireworks object, turn the results into a mongo doc
            for fwid in fws_cmpltd:
                if fwid not in fws:
                    # Get the information from the class we just pulled from the launchpad
                    fw = lpad.get_fw_by_id(fwid)
                    atoms, starting_atoms, trajectory, vasp_settings = get_firework_info(fw)

                    # Initialize the mongo document, doc, and the populate it with the fw info
                    doc = mongo_doc(atoms)
                    doc['initial_configuration'] = mongo_doc(starting_atoms)
                    doc['fwname'] = fw.name
                    doc['fwid'] = fwid
                    doc['directory'] = fw.launches[-1].launch_dir
                    # fw.name['vasp_settings'] = vasp_settings
                    if fw.name['calculation_type'] == 'unit cell optimization':
                        doc['type'] = 'bulk'
                    elif fw.name['calculation_type'] == 'gas phase optimization':
                        doc['type'] = 'gas'
                    # Convert the miller indices from strings to integers
                    if 'miller' in fw.name:
                        if isinstance(fw.name['miller'], str) or isinstance(fw.name['miller'], unicode):
                            doc['fwname']['miller'] = eval(doc['fwname']['miller'])

                    # Write the doc onto the Auxiliary database
                    aux_db.write(doc)
                    print('Dumped a %s firework (FW ID %s) into the Auxiliary DB:' \
                          % (doc['type'], fwid))
                    print_dict(fw.name, indent=1)


class DumpSurfacesToAuxDB(luigi.Task):
    '''
    This class will load the results for surface relaxations from the Primary FireWorks database
    into the Auxiliary vasp.mongo database.
    '''

    def requires(self):
        lpad = get_launchpad()

        # A list of integers containing the Fireworks job ID numbers that have been
        # added to the database already
        with get_aux_db() as aux_db:
            fws = [a['fwid'] for a in aux_db.find({'fwid':{'$exists':True}})]

        # Get all of the completed fireworks for slabs and slab+ads
        fws_cmpltd = lpad.get_fw_ids({'state':'COMPLETED',
                                      'name.calculation_type':'slab optimization'}) + \
                     lpad.get_fw_ids({'state':'COMPLETED',
                                      'name.calculation_type':'slab+adsorbate optimization'})

        # Trouble-shooting code
        #random.seed(42)
        #random.shuffle(fws_cmpltd)
        #fws_cmpltd=fws_cmpltd[-60:]
        fws_cmpltd.reverse()

        # `surfaces` will be a list of the different surfaces that we need to generate before
        # we are able to dump them to the Auxiliary DB.
        surfaces = []
        # `to_dump` will be a list of lists. Each sublist contains information we need to dump
        # a surface from the Primary DB to the Auxiliary DB
        self.to_dump = []
        self.missing_shift_to_dump = []

        # For each fireworks object, turn the results into a mongo doc
        for fwid in fws_cmpltd:
            if fwid not in fws:
                # Get the information from the class we just pulled from the launchpad
                fw = lpad.get_fw_by_id(fwid)
                atoms, starting_atoms, trajectory, vasp_settings = get_firework_info(fw)
                # Prepare to add VASP settings to the doc
                keys = ['gga', 'encut', 'zab_vdw', 'lbeefens', 'luse_vdw', 'pp', 'pp_version']
                settings = OrderedDict()
                for key in keys:
                    if key in vasp_settings:
                        settings[key] = vasp_settings[key]
                # Convert the miller indices from strings to integers
                if isinstance(fw.name['miller'], str) or isinstance(fw.name['miller'], unicode):
                    miller = eval(fw.name['miller'])
                else:
                    miller = fw.name['miller']
                #print(fw.name['mpid'])

                '''
                This next paragraph of code (i.e., the lines until the next blank line)
                addresses our old results that were saved without shift values. Here, we
                re-create a surface so that we can guess what its shift is later on.
                '''
                # Create the surfaces
                if 'shift' not in fw.name:
                    surfaces.append(GenerateSurfaces({'bulk': default_parameter_bulk(mpid=fw.name['mpid'],
                                                                                     settings=settings),
                                                      'slab': default_parameter_slab(miller=miller,
                                                                                     top=True,
                                                                                     shift=0.,
                                                                                     settings=settings)}))
                    self.missing_shift_to_dump.append([atoms, starting_atoms, trajectory,
                                                       vasp_settings, fw, fwid])
                else:

                    # Pass the list of surfaces to dump to `self` so that it can be called by the
                    #`run' method
                    self.to_dump.append([atoms, starting_atoms, trajectory,
                                         vasp_settings, fw, fwid])

        # Establish that we need to create the surfaces before dumping them
        return surfaces

    def run(self):
        selfinput = self.input()

        # Create a class, "aux_db", that has methods to interact with the database.
        with get_aux_db() as aux_db:

            # Start a counter for how many surfaces we will be guessing shifts for
            n_missing_shift = 0

            # Pull out the information for each surface that we put into to_dump
            for atoms, starting_atoms, trajectory, vasp_settings, fw, fwid \
                in self.missing_shift_to_dump + self.to_dump:
                # Initialize the mongo document, doc, and the populate it with the fw info
                doc = mongo_doc(atoms)
                doc['initial_configuration'] = mongo_doc(starting_atoms)
                doc['fwname'] = fw.name
                doc['fwid'] = fwid
                doc['directory'] = fw.launches[-1].launch_dir
                if fw.name['calculation_type'] == 'slab optimization':
                    doc['type'] = 'slab'
                elif fw.name['calculation_type'] == 'slab+adsorbate optimization':
                    doc['type'] = 'slab+adsorbate'
                # Convert the miller indices from strings to integers
                if 'miller' in fw.name:
                    if isinstance(fw.name['miller'], str) or isinstance(fw.name['miller'], unicode):
                        doc['fwname']['miller'] = eval(doc['fwname']['miller'])

                '''
                This next paragraph of code (i.e., the lines until the next blank line)
                addresses our old results that were saved without shift values. Here, we
                guess what the shift is (based on information from the surface we created before
                in the "requires" function) and declare it before saving it to the database.
                '''
                if 'shift' not in doc['fwname']:
                    slab_list_unrelaxed = pickle.load(selfinput[n_missing_shift].open())
                    n_missing_shift += 1
                    atomlist_unrelaxed = [mongo_doc_atoms(slab)
                                          for slab in slab_list_unrelaxed
                                          if slab['tags']['top'] == fw.name['top']]
                    if len(atomlist_unrelaxed) > 1:
                        #pprint(atomlist_unrelaxed)
                        #pprint(fw)
                        # We use the average coordination as a descriptor of the structure,
                        # there should be a pretty large change with different shifts
                        def getCoord(x):
                            return average_coordination_number([AseAtomsAdaptor.get_structure(x)])
                        # Get the coordination for the unrelaxed surface w/ correct shift
                        if doc['type'] == 'slab':
                            reference_coord = getCoord(starting_atoms)
                        elif doc['type'] == 'slab+adsorbate':
                            try:
                                num_adsorbate_atoms = {'':0, 'OH':2, 'CO':2, 'C':1, 'H':1, 'O':1}[fw.name['adsorbate']]
                            except KeyError:
                                print("%s is not recognizable by GASpy's adsorbates dictionary. \
                                      Please add it to `num_adsorbate_atoms` \
                                      in `DumpSurfacesToAuxDB`" % fw.name['adsorbate'])
                            if num_adsorbate_atoms > 0:
                                starting_blank = starting_atoms[0:-num_adsorbate_atoms]
                            else:
                                starting_blank = starting_atoms
                            reference_coord = getCoord(starting_blank)
                        # Get the coordination for each unrelaxed surface
                        unrelaxed_coord = map(getCoord, atomlist_unrelaxed)
                        # We want to minimize the distance in these dictionaries
                        def getDist(x, y):
                            vals = []
                            for key in x:
                                vals.append(x[key]-y[key])
                            return np.linalg.norm(vals)
                        # Get the distances to the reference coordinations
                        dist = map(lambda x: getDist(x, reference_coord), unrelaxed_coord)
                        # Grab the atoms object that minimized this distance
                        shift = slab_list_unrelaxed[np.argmin(dist)]['tags']['shift']
                        doc['fwname']['shift'] = float(np.round(shift, 4))
                        doc['fwname']['shift_guessed'] = True
                    else:
                        doc['fwname']['shift'] = 0
                        doc['fwname']['shift_guessed'] = True

                aux_db.write(doc)
                print('Dumped a %s firework (FW ID %s) into the Auxiliary DB:' \
                      % (doc['type'], fwid))
                print_dict(fw.name, indent=1)

        # Touch the token to indicate that we've written to the database
        with self.output().temporary_path() as self.temp_output_path:
            with open(self.temp_output_path, 'w') as fhandle:
                fhandle.write(' ')

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/DumpToAuxDB.token')


class UpdateAllDB(luigi.WrapperTask):
    '''
    First, dump from the Primary database to the Auxiliary database.
    Then, dump from the Auxiliary database to the Local adsorption energy database.
    Finally, re-request the adsorption energies to re-initialize relaxations & FW submissions.
    '''
    # write_db is a boolean. If false, we only execute FingerprintRelaxedAdslabs, which
    # submits calculations to Fireworks (if needed). If writeDB is true, then we still
    # exectute FingerprintRelaxedAdslabs, but we also dump to the Local DB.
    writeDB = luigi.BoolParameter(False)
    # max_processes is the maximum number of calculation sets to dump. If it's set to zero,
    # then there is no limit. This is used to limit the scope of a DB update for
    # debugging purposes.
    max_processes = luigi.IntParameter(0)
    def requires(self):
        '''
        Luigi automatically runs the `requires` method whenever we tell it to execute a
        class. Since we are not truly setting up a dependency (i.e., setting up `requires`,
        `run`, and `output` methods), we put all of the "action" into the `requires`
        method.
        '''

        # Dump from the Primary DB to the Aux DB
        DumpBulkGasToAuxDB().run()
        yield DumpSurfacesToAuxDB()

        # Get every row in the Aux database
        rows = get_aux_db().find({'type':'slab+adsorbate'})
        # Get all of the current fwid entries in the local DB
        with connect(LOCAL_DB_PATH+'/adsorption_energy_database.db') as enrg_db:
            fwids = [row.fwid for row in enrg_db.select()]

        # For each adsorbate/configuration, make a task to write the results to the output database
        for i, row in enumerate(rows):
            # Break the loop if we reach the maxmimum number of processes
            if i+1 == self.max_processes:
                break

            # Only make the task if 1) the fireworks task is not already in the database,
            # 2) there is an adsorbate, and 3) we haven't reached the (non-zero) limit of rows
            # to dump.
            if (row['fwid'] not in fwids
                    and row['fwname']['adsorbate'] != ''
                    and ((self.max_processes == 0) or (self.max_processes > 0 and i < self.max_processes))):
                # Pull information from the Aux DB
                mpid = row['fwname']['mpid']
                miller = row['fwname']['miller']
                adsorption_site = row['fwname']['adsorption_site']
                adsorbate = row['fwname']['adsorbate']
                top = row['fwname']['top']
                num_slab_atoms = row['fwname']['num_slab_atoms']
                slabrepeat = row['fwname']['slabrepeat']
                shift = row['fwname']['shift']
                keys = ['gga', 'encut', 'zab_vdw', 'lbeefens', 'luse_vdw', 'pp', 'pp_version']
                settings = OrderedDict()
                for key in keys:
                    if key in row['fwname']['vasp_settings']:
                        settings[key] = row['fwname']['vasp_settings'][key]
                # Create the nested dictionary of information that we will store in the Aux DB
                parameters = {'bulk':default_parameter_bulk(mpid, settings=settings),
                              'gas':default_parameter_gas(gasname='CO', settings=settings),
                              'slab':default_parameter_slab(miller=miller,
                                                            shift=shift,
                                                            top=top,
                                                            settings=settings),
                              'adsorption':default_parameter_adsorption(adsorbate=adsorbate,
                                                                        num_slab_atoms=num_slab_atoms,
                                                                        slabrepeat=slabrepeat,
                                                                        adsorption_site=adsorption_site,
                                                                        settings=settings)}

                # Flag for hitting max_dump
                if i+1 == self.max_processes:
                    print('Reached the maximum number of processes, %s' % self.max_processes)
                # Dump to the local DB if we told Luigi to do so. We may do so by adding the
                # `--writeDB` flag when calling Luigi. If we do not dump to the local DB, then
                # we fingerprint the slab+adsorbate system
                if self.writeDB:
                    yield DumpToLocalDB(parameters)
                else:
                    yield FingerprintRelaxedAdslab(parameters)


class GenerateBulk(luigi.Task):
    '''
    This class pulls a bulk structure from Materials Project and then converts it to an ASE atoms
    object
    '''
    parameters = luigi.DictParameter()

    def run(self):
        # Connect to the Materials Project database
        with MPRester("MGOdX3P4nI18eKvE") as m:
            # Pull out the PyMatGen structure and convert it to an ASE atoms object
            structure = m.get_structure_by_material_id(self.parameters['bulk']['mpid'])
            atoms = AseAtomsAdaptor.get_atoms(structure)
            # Dump the atoms object into our pickles
            with self.output().temporary_path() as self.temp_output_path:
                pickle.dump([mongo_doc(atoms)], open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class GenerateGas(luigi.Task):
    parameters = luigi.DictParameter()

    def run(self):
        atoms = g2[self.parameters['gas']['gasname']]
        atoms.positions += 10.
        atoms.cell = [20, 20, 20]
        atoms.pbc = [True, True, True]
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump([mongo_doc(atoms)], open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class GenerateSurfaces(luigi.Task):
    '''
    This class uses PyMatGen to create surfaces (i.e., slabs cut from a bulk) from ASE atoms objects
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        If the bulk does not need to be relaxed, we simply pull it from Materials Project using
        GenerateBulk. If it needs to be relaxed, then we submit it to Fireworks.
        '''
        if 'unrelaxed' in self.parameters and self.parameters['unrelaxed']:
            return GenerateBulk(parameters={'bulk':self.parameters['bulk']})
        else:
            return SubmitToFW(calctype='bulk', parameters={'bulk':self.parameters['bulk']})

    def run(self):
        # Preparation work with ASE and PyMatGen before we start creating the slabs
        atoms = mongo_doc_atoms(pickle.load(self.input().open())[0])
        structure = AseAtomsAdaptor.get_structure(atoms)
        sga = SpacegroupAnalyzer(structure, symprec=0.1)
        structure = sga.get_conventional_standard_structure()
        gen = SlabGenerator(structure,
                            self.parameters['slab']['miller'],
                            **self.parameters['slab']['slab_generate_settings'])
        slabs = gen.get_slabs(**self.parameters['slab']['get_slab_settings'])
        slabsave = []
        for slab in slabs:
            # If this slab is the only one in the set with this miller index, then the shift doesn't
            # matter... so we set the shift as zero.
            if len([a for a in slabs if a.miller_index == slab.miller_index]) == 1:
                shift = 0
            else:
                shift = slab.shift

            # Create an atoms class for this particular slab, "atoms_slab"
            atoms_slab = AseAtomsAdaptor.get_atoms(slab)
            # Then reorient the "atoms_slab" class so that the surface of the slab is pointing
            # upwards in the z-direction
            rotate(atoms_slab,
                   atoms_slab.cell[2], (0, 0, 1),
                   atoms_slab.cell[0], [1, 0, 0],
                   rotate_cell=True)
            # Save the slab, but only if it isn't already in the database
            top = True
            tags = {'type':'slab',
                    'top':top,
                    'mpid':self.parameters['bulk']['mpid'],
                    'miller':self.parameters['slab']['miller'],
                    'shift':shift,
                    'num_slab_atoms':len(atoms_slab),
                    'relaxed':False,
                    'slab_generate_settings':self.parameters['slab']['slab_generate_settings'],
                    'get_slab_settings':self.parameters['slab']['get_slab_settings']}
            slabdoc = mongo_doc(constrain_slab(atoms_slab, len(atoms_slab)))
            slabdoc['tags'] = tags
            slabsave.append(slabdoc)

            # If the top of the cut is not identical to the bottom, then save the bottom slab to the
            # database, as well. To do this, we first pull out the sga class of this particular
            # slab, "sga_slab". Again, we use a symmetry finding tolerance of 0.1 to be consistent
            # with MP
            sga_slab = SpacegroupAnalyzer(slab, symprec=0.1)
            # Then use the "sga_slab" class to create a list, "symm_ops", that contains classes,
            # which contain matrix and vector operators that may be used to rotate/translate the
            # slab about axes of symmetry
            symm_ops = sga_slab.get_symmetry_operations()
            # Create a boolean, "z_invertible", which will be "True" if the top of the slab is
            # the same as the bottom.
            z_invertible = True in map(lambda x: x.as_dict()['matrix'][2][2] == -1, symm_ops)
            # If the bottom is different, then...
            if not z_invertible:
                # flip the slab upside down...
                atoms_slab.rotate('x', math.pi, rotate_cell=True)

                # and if it is not in the database, then save it.
                slabdoc = mongo_doc(constrain_slab(atoms_slab, len(atoms_slab)))
                tags = {'type':'slab',
                        'top':not(top),
                        'mpid':self.parameters['bulk']['mpid'],
                        'miller':self.parameters['slab']['miller'],
                        'shift':shift,
                        'num_slab_atoms':len(atoms_slab),
                        'relaxed':False,
                        'slab_generate_settings':self.parameters['slab']['slab_generate_settings'],
                        'get_slab_settings':self.parameters['slab']['get_slab_settings']}
                slabdoc['tags'] = tags
                slabsave.append(slabdoc)

        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump(slabsave, open(self.temp_output_path, 'w'))

        return

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class GenerateSiteMarkers(luigi.Task):
    '''
    This class will take a set of slabs, enumerate the adsorption sites on the slab, add a marker
    on the sites (i.e., Uranium), and then save the Uranium+slab systems into our pickles
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        If the system we are trying to create markers for is unrelaxed, then we only need
        to create the bulk and surfaces. If the system should be relaxed, then we need to
        submit the bulk and the slab to Fireworks.
        '''
        if 'unrelaxed' in self.parameters and self.parameters['unrelaxed']:
            return [GenerateSurfaces(parameters=OrderedDict(unrelaxed=True,
                                                            bulk=self.parameters['bulk'],
                                                            slab=self.parameters['slab'])),
                    GenerateBulk(parameters={'bulk':self.parameters['bulk']})]
        else:
            return [SubmitToFW(calctype='slab',
                               parameters=OrderedDict(bulk=self.parameters['bulk'],
                                                      slab=self.parameters['slab'])),
                    SubmitToFW(calctype='bulk',
                               parameters={'bulk':self.parameters['bulk']})]

    def run(self):
        # Defire our marker, a uraniom Atoms object. Then pull out the slabs and bulk
        adsorbate = {'name':'U', 'atoms':Atoms('U')}
        slabs = pickle.load(self.input()[0].open())
        bulk = mongo_doc_atoms(pickle.load(self.input()[1].open())[0])

        # Initialize `adslabs_to_save`, which will be a list containing marked slabs (i.e.,
        # adslabs) for us to save
        adslabs_to_save = []
        for slab in slabs:
            # "slab_atoms" [atoms class] is the first slab structure in Aux DB that corresponds
            # to the slab that we are looking at. Note that thise any possible repeats of the slab
            # in the database.
            slab_atoms = mongo_doc_atoms(slab)

            # Repeat the atoms in the slab to get a cell that is at least as large as the "mix_xy"
            # parameter we set above.
            nx = int(ceil(self.parameters['adsorption']['min_xy']/norm(slab_atoms.cell[0])))
            ny = int(ceil(self.parameters['adsorption']['min_xy']/norm(slab_atoms.cell[1])))
            slabrepeat = (nx, ny, 1)
            slab_atoms.info['adsorbate_info'] = ''
            slab_atoms_repeat = slab_atoms.repeat(slabrepeat)

            # Find the adsorption sites. Then for each site we find, we create a dictionary
            # of tags to describe the site. Then we save the tags to our pickles.
            sites = find_adsorption_sites(slab_atoms, bulk)
            for site in sites:
                # Populate the `tags` dictionary with various information
                if 'unrelaxed' in self.parameters:
                    shift = slab['tags']['shift']
                    top = slab['tags']['top']
                    miller = slab['tags']['miller']
                else:
                    shift = self.parameters['slab']['shift']
                    top = self.parameters['slab']['top']
                    miller = self.parameters['slab']['miller']
                tags = {'type':'slab+adsorbate',
                            'adsorption_site':str(np.round(site, decimals=2)),
                            'slabrepeat':str(slabrepeat),
                            'adsorbate':adsorbate['name'],
                            'top':top,
                            'miller':miller,
                            'shift':shift,
                            'relaxed':False}
                # Then add the adsorbate marker on top of the slab. Note that we use a local,
                # deep copy of the marker because the marker was created outside of this loop.
                _adsorbate = adsorbate['atoms'].copy()
                # Move the adsorbate onto the adsorption site...
                _adsorbate.translate(site)
                # Put the adsorbate onto the slab and add the adslab system to the tags
                adslab = slab_atoms_repeat.copy() + _adsorbate
                tags['atoms'] = adslab

                # Finally, add the information to list of things to save
                adslabs_to_save.append(tags)

        # Save the marked systems to our pickles
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump(adslabs_to_save, open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class GenerateAdslabs(luigi.Task):
    '''
    This class takes a set of adsorbate positions from GenerateSiteMarkers and replaces
    the marker (a uranium atom) with the correct adsorbate. Adding an adsorbate is done in two
    steps (marker enumeration, then replacement) so that the hard work of enumerating all
    adsorption sites is only done once and reused for every adsorbate
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        We need the generated adsorbates with the marker atoms.  We delete
        parameters['adsorption']['adsorbates'] so that every generate_adsorbates_marker call
        looks the same, even with different adsorbates requested in this task
        '''
        parameters_no_adsorbate = copy.deepcopy(self.parameters)
        del parameters_no_adsorbate['adsorption']['adsorbates']
        return GenerateSiteMarkers(parameters_no_adsorbate)

    def run(self):
        # Load the configurations
        adsorbate_configs = pickle.load(self.input().open())

        # For each configuration replace the marker with the adsorbate
        for adsorbate_config in adsorbate_configs:
            # Load the atoms object for the slab and adsorbate
            slab = adsorbate_config['atoms']
            ads = pickle.loads(self.parameters['adsorption']['adsorbates'][0]['atoms'].decode('hex'))
            # Find the position of the marker/adsorbate and the number of slab atoms, which we will
            # use later
            ads_pos = slab[-1].position
            num_slab_atoms = len(slab)
            # Delete the marker on the slab, and then put the adsorbate onto it
            del slab[-1]
            ads.translate(ads_pos)
            adslab = slab + ads
            # Set constraints and update the list of dictionaries with the correct atoms
            # object adsorbate name
            adslab.set_constraint()
            adsorbate_config['atoms'] = constrain_slab(adslab, num_slab_atoms)
            adsorbate_config['adsorbate'] = self.parameters['adsorption']['adsorbates'][0]['name']

        # Save the generated list of adsorbate configurations to a pkl file
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump(adsorbate_configs, open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class CalculateEnergy(luigi.Task):
    '''
    This class attempts to return the adsorption energy of a configuration relative to
    stoichiometric amounts of CO, H2, H2O
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        We need the relaxed slab, the relaxed slab+adsorbate, and relaxed CO/H2/H2O gas
        structures/energies
        '''
        # Initialize the list of things that need to be done before we can calculate the
        # adsorption enegies
        toreturn = []

        # First, we need to relax the slab+adsorbate system
        toreturn.append(SubmitToFW(parameters=self.parameters, calctype='slab+adsorbate'))

        # Then, we need to relax the slab. We do this by taking the adsorbate off and
        # replacing it with '', i.e., nothing. It's still labeled as a 'slab+adsorbate'
        # calculation because of our code infrastructure.
        param = copy.deepcopy(self.parameters)
        param['adsorption']['adsorbates'] = [OrderedDict(name='', atoms=pickle.dumps(Atoms('')).encode('hex'))]
        toreturn.append(SubmitToFW(parameters=param, calctype='slab+adsorbate'))

        # Lastly, we need to relax the base gases.
        for gasname in ['CO', 'H2', 'H2O']:
            param = copy.deepcopy({'gas':self.parameters['gas']})
            param['gas']['gasname'] = gasname
            toreturn.append(SubmitToFW(parameters=param, calctype='gas'))

        # Now we put it all together.
        #print('Checking for/submitting relaxations for %s %s' % (self.parameters['bulk']['mpid'], self.parameters['slab']['miller']))
        return toreturn

    def run(self):
        inputs = self.input()

        # Load the gas phase energies
        gasEnergies = {}
        gasEnergies['CO'] = mongo_doc_atoms(pickle.load(inputs[2].open())[0]).get_potential_energy()
        gasEnergies['H2'] = mongo_doc_atoms(pickle.load(inputs[3].open())[0]).get_potential_energy()
        gasEnergies['H2O'] = mongo_doc_atoms(pickle.load(inputs[4].open())[0]).get_potential_energy()

        # Load the slab+adsorbate relaxed structures, and take the lowest energy one
        slab_ads = pickle.load(inputs[0].open())
        lowest_energy_slab = np.argmin(map(lambda x: mongo_doc_atoms(x).get_potential_energy(), slab_ads))
        slab_ads_energy = mongo_doc_atoms(slab_ads[lowest_energy_slab]).get_potential_energy()

        # Load the slab relaxed structures, and take the lowest energy one
        slab_blank = pickle.load(inputs[1].open())
        lowest_energy_blank = np.argmin(map(lambda x: mongo_doc_atoms(x).get_potential_energy(), slab_blank))
        slab_blank_energy = np.min(map(lambda x: mongo_doc_atoms(x).get_potential_energy(), slab_blank))

        # Get the per-atom energies as a linear combination of the basis set
        mono_atom_energies = {'H':gasEnergies['H2']/2.,
                              'O':gasEnergies['H2O']-gasEnergies['H2'],
                              'C':gasEnergies['CO']-(gasEnergies['H2O']-gasEnergies['H2'])}

        # Get the total energy of the stoichiometry amount of gas reference species
        gas_energy = 0
        for ads in self.parameters['adsorption']['adsorbates']:
            gas_energy += np.sum(map(lambda x: mono_atom_energies[x],
                                     ads_dict(ads['name']).get_chemical_symbols()))

        # Calculate the adsorption energy
        dE = slab_ads_energy - slab_blank_energy - gas_energy

        # Make an atoms object with a single-point calculator that contains the potential energy
        adjusted_atoms = mongo_doc_atoms(slab_ads[lowest_energy_slab])
        adjusted_atoms.set_calculator(SinglePointCalculator(adjusted_atoms,
                                                            forces=adjusted_atoms.get_forces(),
                                                            energy=dE))

        # Write a dictionary with the results and the entries that were used for the calculations
        # so that fwid/etc for each can be recorded
        towrite = {'atoms':adjusted_atoms,
                   'slab+ads':slab_ads[lowest_energy_slab],
                   'slab':slab_blank[lowest_energy_blank],
                   'gas':{'CO':pickle.load(inputs[2].open())[0],
                          'H2':pickle.load(inputs[3].open())[0],
                          'H2O':pickle.load(inputs[4].open())[0]}
                  }

        # Write the dictionary as a pickle
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump(towrite, open(self.temp_output_path, 'w'))

        for ads in self.parameters['adsorption']['adsorbates']:
            print('Finished CalculateEnergy for %s on the %s site of %s %s:  %s eV' \
                  % (ads['name'],
                     self.parameters['adsorption']['adsorbates'][0]['adsorption_site'],
                     self.parameters['bulk']['mpid'],
                     self.parameters['slab']['miller'],
                     dE))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class FingerprintRelaxedAdslab(luigi.Task):
    '''
    This class takes relaxed structures from our Pickles, fingerprints them, then adds the
    fingerprints back to our Pickles
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        Our first requirement is CalculateEnergy, which relaxes the slab+ads system. Our second
        requirement is to relax the slab+ads system again, but without the adsorbates. We do this
        to ensure that the "blank slab" we are using in the adsorption calculations has the same
        number of slab atoms as the slab+ads system.
        '''
        # Here, we take the adsorbate off the slab+ads system
        param = copy.deepcopy(self.parameters)
        param['adsorption']['adsorbates'] = [OrderedDict(name='',
                                                         atoms=pickle.dumps(Atoms('')).
                                                         encode('hex'))]
        return [CalculateEnergy(self.parameters),
                SubmitToFW(parameters=param,
                           calctype='slab+adsorbate')]

    def run(self):
        ''' We fingerprint the slab+adsorbate system both before and after relaxation. '''
        # Load the atoms objects for the lowest-energy slab+adsorbate (adslab) system and the
        # blank slab (slab)
        adslab = pickle.load(self.input()[0].open())
        slab = pickle.load(self.input()[1].open())

        # The atoms object for the adslab prior to relaxation
        adslab0 = mongo_doc_atoms(adslab['slab+ads']['initial_configuration'])
        # The number of atoms in the slab also happens to be the index for the first atom
        # of the adsorbate (in the adslab system)
        slab_natoms = slab[0]['atoms']['natoms']
        ads_ind = slab_natoms

        # If our "adslab" system actually doesn't have an adsorbate, then do not fingerprint
        if slab_natoms == len(adslab['atoms']):
            fp_final = {}
            fp_init = {}
        else:
            # Calculate fingerprints for the initial and final state
            fp_final = fingerprint(adslab['atoms'], ads_ind)
            fp_init = fingerprint(adslab0, ads_ind)

        # Save the the fingerprints of the final and initial state as a list in a pickle file
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump([fp_final, fp_init], open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class FingerprintUnrelaxedAdslabs(luigi.Task):
    '''
    This class takes unrelaxed slab+adsorbate (adslab) systems from our pickles, fingerprints the
    adslab, fingerprints the slab (without an adsorbate), and then adds fingerprints back to our
    Pickles. Note that we fingerprint the slab because we may have had to repeat the original slab
    to add the adsorbate onto it, and if so then we also need to fingerprint the repeated slab.
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        We call the GenerateAdslabs class twice; once for the adslab, and once for the slab
        '''
        # Make a copy of `parameters` for our slab, but then we take off the adsorbate
        param_slab = copy.deepcopy(self.parameters)
        param_slab['adsorption']['adsorbates'] = [OrderedDict(name='', atoms=pickle.dumps(Atoms('')).encode('hex'))]
        return [GenerateAdslabs(self.parameters),
                GenerateAdslabs(parameters=param_slab)]

    def run(self):
        # Load the list of slab+adsorbate (adslab) systems, and the bare slab. Also find the number
        # of slab atoms
        adslabs = pickle.load(self.input()[0].open())
        slab = pickle.load(self.input()[1].open())
        expected_slab_atoms = len(slab[0]['atoms'])
        # len(slabs[0]['atoms']['atoms'])*np.prod(eval(adslabs[0]['slabrepeat']))

        # Fingerprint each adslab
        for adslab in adslabs:
            # Don't bother if the adslab happens to be bare
            if adslab['adsorbate'] == '':
                fp = {}
            else:
                fp = fingerprint(adslab['atoms'], expected_slab_atoms)
            # Add the fingerprints to the dictionary
            for key in fp:
                adslab[key] = fp[key]

        # Write
        with self.output().temporary_path() as self.temp_output_path:
            pickle.dump(adslabs, open(self.temp_output_path, 'w'))

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class DumpToLocalDB(luigi.Task):
    ''' This class dumps the adsorption energies from our pickles to our Local energies DB '''
    parameters = luigi.DictParameter()

    def requires(self):
        '''
        We want the lowest energy structure (with adsorption energy), the fingerprinted structure,
        and the bulk structure
        '''
        return [CalculateEnergy(self.parameters),
                FingerprintRelaxedAdslab(self.parameters),
                SubmitToFW(calctype='bulk',
                           parameters={'bulk':self.parameters['bulk']})]

    def run(self):
        # Load the structure
        best_sys_pkl = pickle.load(self.input()[0].open())
        # Extract the atoms object
        best_sys = best_sys_pkl['atoms']
        # Get the lowest energy bulk structure
        bulk = pickle.load(self.input()[2].open())
        bulkmin = np.argmin(map(lambda x: x['results']['energy'], bulk))
        # Load the fingerprints of the initial and final state
        fingerprints = pickle.load(self.input()[1].open())
        fp_final = fingerprints[0]
        fp_init = fingerprints[1]
        for fp in [fp_init, fp_final]:
            for key in ['neighborcoord', 'nextnearestcoordination', 'coordination']:
                if key not in fp:
                    fp[key] = ''

        # Create and use tools to calculate the angle between the bond length of the diatomic
        # adsorbate and the z-direction of the bulk. We are not currently calculating triatomics
        # or larger.
        def unit_vector(vector):
            ''' Returns the unit vector of the vector.  '''
            return vector / np.linalg.norm(vector)
        def angle_between(v1, v2):
            ''' Returns the angle in radians between vectors 'v1' and 'v2'::  '''
            v1_u = unit_vector(v1)
            v2_u = unit_vector(v2)
            return np.arccos(np.clip(np.dot(v1_u, v2_u), -1.0, 1.0))
        if self.parameters['adsorption']['adsorbates'][0]['name'] in ['CO', 'OH']:
            angle = angle_between(best_sys[-1].position-best_sys[-2].position, best_sys.cell[2])
            if self.parameters['slab']['top'] is False:
                angle = np.abs(angle - math.pi)
        else:
            angle = 0.
        angle = angle/2./np.pi*360

        '''
        Calculate the maximum movement of surface atoms during the relaxation. then we do it again,
        but for adsorbate atoms.
        '''
        # First, calculate the number of adsorbate atoms
        num_adsorbate_atoms = len(pickle.loads(self.parameters['adsorption']['adsorbates'][0]['atoms'].decode('hex')))
        # Get just the slab atoms of the initial and final state
        slab_atoms_final = best_sys[0:-num_adsorbate_atoms]
        slab_atoms_initial = mongo_doc_atoms(best_sys_pkl['slab+ads']['initial_configuration'])[0:-num_adsorbate_atoms]
        # Calculate the distances for each atom
        distances = slab_atoms_final.positions - slab_atoms_initial.positions
        # Reduce the distances in case atoms wrapped around (the minimum image convention)
        dist, Dlen = find_mic(distances, slab_atoms_final.cell, slab_atoms_final.pbc)
        # Calculate the max movement of the surface atoms
        max_surface_movement = np.max(Dlen)
        # Repeat the procedure, but for adsorbates
        # get just the slab atoms of the initial and final state
        adsorbate_atoms_final = best_sys[-num_adsorbate_atoms:]
        adsorbate_atoms_initial = mongo_doc_atoms(best_sys_pkl['slab+ads']['initial_configuration'])[-num_adsorbate_atoms:]
        distances = adsorbate_atoms_final.positions - adsorbate_atoms_initial.positions
        dist, Dlen = find_mic(distances, slab_atoms_final.cell, slab_atoms_final.pbc)
        max_adsorbate_movement = np.max(Dlen)

        # Make a dictionary of tags to add to the database
        criteria = {'type':'slab+adsorbate',
                    'mpid':self.parameters['bulk']['mpid'],
                    'miller':'(%d.%d.%d)'%(self.parameters['slab']['miller'][0],
                                           self.parameters['slab']['miller'][1],
                                           self.parameters['slab']['miller'][2]),
                    'num_slab_atoms':self.parameters['adsorption']['num_slab_atoms'],
                    'top':self.parameters['slab']['top'],
                    'slabrepeat':self.parameters['adsorption']['slabrepeat'],
                    'relaxed':True,
                    'adsorbate':self.parameters['adsorption']['adsorbates'][0]['name'],
                    'adsorption_site':self.parameters['adsorption']['adsorbates'][0]['adsorption_site'],
                    'coordination':fp_final['coordination'],
                    'nextnearestcoordination':fp_final['nextnearestcoordination'],
                    'neighborcoord':str(fp_final['neighborcoord']),
                    'initial_coordination':fp_init['coordination'],
                    'initial_nextnearestcoordination':fp_init['nextnearestcoordination'],
                    'initial_neighborcoord':str(fp_init['neighborcoord']),
                    'shift':best_sys_pkl['slab+ads']['fwname']['shift'],
                    'fwid':best_sys_pkl['slab+ads']['fwid'],
                    'slabfwid':best_sys_pkl['slab']['fwid'],
                    'bulkfwid':bulk[bulkmin]['fwid'],
                    'adsorbate_angle':angle,
                    'max_surface_movement':max_surface_movement,
                    'max_adsorbate_movement':max_adsorbate_movement}
        # Turn the appropriate VASP tags into [str] so that ase-db may accept them.
        VSP_STNGS = vasp_settings_to_str(self.parameters['adsorption']['vasp_settings'])
        for key in VSP_STNGS:
            if key == 'pp_version':
                criteria[key] = VSP_STNGS[key] + '.'
            else:
                criteria[key] = VSP_STNGS[key]

        # Write the entry into the database
        with connect(LOCAL_DB_PATH+'/adsorption_energy_database.db') as conAds:
            conAds.write(best_sys, **criteria)

        # Write a blank token file to indicate this was done so that the entry is not written again
        with self.output().temporary_path() as self.temp_output_path:
            with open(self.temp_output_path, 'w') as fhandle:
                fhandle.write(' ')

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))


class UpdateEnumerations(luigi.Task):
    '''
    This class re-requests the enumeration of adsorption sites to re-initialize our various
    generating functions. It then dumps any completed site enumerations into our Local DB
    for adsorption sites.
    '''
    parameters = luigi.DictParameter()

    def requires(self):
        ''' Get the generated adsorbate configurations '''
        return FingerprintUnrelaxedAdslabs(self.parameters)

    def run(self):
        with connect(LOCAL_DB_PATH+'/enumerated_adsorption_sites.db') as con:
            # Load the configurations
            configs = pickle.load(self.input().open())
            # Find the unique configurations based on the fingerprint of each site
            unq_configs, unq_inds = np.unique(map(lambda x: str([x['shift'],
                                                                 x['coordination'],
                                                                 x['neighborcoord']]),
                                                  configs),
                                              return_index=True)
            # For each configuration, write a row to the database
            for i in unq_inds:
                config = configs[i]
                con.write(config['atoms'],
                          shift=config['shift'],
                          miller=str(self.parameters['slab']['miller']),
                          mpid=self.parameters['bulk']['mpid'],
                          adsorbate=self.parameters['adsorption']['adsorbates'][0]['name'],
                          top=config['top'],
                          adsorption_site=config['adsorption_site'],
                          coordination=config['coordination'],
                          neighborcoord=str(config['neighborcoord']),
                          nextnearestcoordination=str(config['nextnearestcoordination']))
        # Write a token file to indicate this task has been completed and added to the DB
        with self.output().temporary_path() as self.temp_output_path:
            with open(self.temp_output_path, 'w') as fhandle:
                fhandle.write(' ')

    def output(self):
        return luigi.LocalTarget(LOCAL_DB_PATH+'/pickles/%s.pkl'%(self.task_id))
