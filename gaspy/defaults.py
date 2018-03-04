'''
This modules contains default settings for various function and queries used in GASpy and its
submodules.
'''

import pdb  # noqa: F401
import copy
from collections import OrderedDict
import cPickle as pickle
from ase import Atoms
import ase.constraints
from vasp import Vasp


def fingerprints(simulated=False):
    '''
    WARNING:  A lot of code depends on this. Do not add any queries that rely on final
    fingerprinting information. Do not take anything out without thinking real hard
    about it. Adding stuff is probably ok, but only if the query works on the catalog,
    as well.

    Returns a dictionary that is meant to be passed to mongo aggregators to create
    new mongo docs. The keys here are the keys for the new mongo doc, and the values
    are where you can find the information from the old mongo docs (in our databases).
    This function pairs well with the `gaspy.utils.get_cursor` function.

    Note that our code implicitly assumes an identical document structure between all
    of the collections that it looks at.

    Input:
        simulated   A boolean indicating whether or not you want the fingerprints
                    of a simulated (on non-simulated) datum.
    '''
    if simulated:
        fingerprints = {'mongo_id': '$_id',
                        'mpid': '$processed_data.calculation_info.mpid',
                        'formula': '$processed_data.calculation_info.formula',
                        'miller': '$processed_data.calculation_info.miller',
                        'shift': '$processed_data.calculation_info.shift',
                        'top': '$processed_data.calculation_info.top',
                        'coordination': '$processed_data.fp_final.coordination',
                        'neighborcoord': '$processed_data.fp_final.neighborcoord',
                        'nextnearestcoordination': '$processed_data.fp_final.nextnearestcoordination',
                        'energy': '$results.energy',
                        'adsorbates': '$processed_data.calculation_info.adsorbate_names'}
    else:
        fingerprints = {'mongo_id': '$_id',
                        'mpid': '$processed_data.calculation_info.mpid',
                        'formula': '$processed_data.calculation_info.formula',
                        'miller': '$processed_data.calculation_info.miller',
                        'shift': '$processed_data.calculation_info.shift',
                        'top': '$processed_data.calculation_info.top',
                        'coordination': '$processed_data.fp_init.coordination',
                        'neighborcoord': '$processed_data.fp_init.neighborcoord',
                        'nextnearestcoordination': '$processed_data.fp_init.nextnearestcoordination'}
    return fingerprints


def xc_settings(xc):
    '''
    This function is where we populate the default calculation settings we want for each
    specific xc (exchange correlational)
    '''
    # If we choose `rpbe`, then define default calculations that are different from
    # what Vaspy recommends.
    if xc == 'rpbe':
        settings = OrderedDict(gga='RP', pp='PBE')
    # Otherwise, simply listen to Vaspy
    else:
        settings = OrderedDict(Vasp.xc_defaults[xc])

    return settings


def calc_settings(xc):
    '''
    This function defines the default calculational settings for GASpy to use
    '''
    # Standard settings to use regardless of xc (exchange correlational)
    settings = OrderedDict({'encut': 350, 'pp_version': '5.4'})

    # Call on the xc_settings function to define the rest of the settings
    default_settings = xc_settings(xc)
    for key in default_settings:
        settings[key] = default_settings[key]

    return settings


def gas_parameters(gasname, settings='beef-vdw'):
    '''
    Generate some default parameters for a gas and expected relaxation settings

    Inputs:
        gasname     A string containing the name of the gas
        settings    A string that Vaspy can use to create vasp settings.
                    Or `rpbe` if we want to use that
    '''
    # calc_settings returns a default set of calculational settings, but only if
    # the `settings` argument is a string.
    if isinstance(settings, str):
        settings = calc_settings(settings)

    return OrderedDict(gasname=gasname,
                       relaxed=True,
                       vasp_settings=OrderedDict(ibrion=2,
                                                 nsw=100,
                                                 isif=0,
                                                 kpts=[1, 1, 1],
                                                 ediffg=-0.03,
                                                 **settings))


def bulk_parameters(mpid, settings='beef-vdw', encut=500., max_atoms=50):
    '''
    Generate some default parameters for a bulk and expected relaxation settings

    Inputs:
        gasname     A string containing the name of the gas
        settings    A string that Vaspy can use to create vasp settings.
                    Or `rpbe` if we want to use that
        encut       The energy cut-off
    '''
    # calc_settings returns a default set of calculational settings, but only if
    # the `settings` argument is a string.
    if isinstance(settings, str):
        settings = calc_settings(settings)

    # We're getting a handle to a dictionary, so need to copy before modifying
    settings = copy.deepcopy(settings)
    settings['encut'] = encut
    return OrderedDict(mpid=mpid,
                       relaxed=True,
                       max_atoms=max_atoms,
                       vasp_settings=OrderedDict(ibrion=1,
                                                 nsw=100,
                                                 isif=7,
                                                 isym=0,
                                                 ediff=1e-8,
                                                 kpts=[10, 10, 10],
                                                 prec='Accurate',
                                                 **settings))


def slab_parameters(miller, top, shift, settings='beef-vdw'):
    '''
    Generate some default parameters for a slab and expected relaxation settings

    Inputs:
        miller      A list of the three miller indices of the slab
        top         A boolean stating whether or not the "top" of the slab is pointing upwards
        shift       As per PyMatGen, the shift is the distance of the planar translation
                    in the z-direction (after the cut). Look up PyMatGen for more details.
        settings    A string that Vaspy can use to create vasp settings.
                    Or `rpbe` if we want to use that
    '''
    # calc_settings returns a default set of calculational settings, but only if
    # the `settings` argument is a string.
    if isinstance(settings, str):
        settings = calc_settings(settings)

    return OrderedDict(miller=miller,
                       top=top,
                       max_miller=2,
                       shift=shift,
                       relaxed=True,
                       vasp_settings=OrderedDict(ibrion=2,
                                                 nsw=100,
                                                 isif=0,
                                                 isym=0,
                                                 kpts=[4, 4, 1],
                                                 lreal='Auto',
                                                 ediffg=-0.03,
                                                 **settings),
                       slab_generate_settings=OrderedDict(min_slab_size=7.,
                                                          min_vacuum_size=20.,
                                                          lll_reduce=False,
                                                          center_slab=True,
                                                          primitive=True,
                                                          max_normal_search=1),
                       get_slab_settings=OrderedDict(tol=0.3,
                                                     bonds=None,
                                                     max_broken_bonds=0,
                                                     symmetrize=False))


def adsorbates_dict():
    '''
    This function is intended to be used to generate (and store) a library of adsorbates
    in dictionary form, where the key is a string for the adsorbate and the value is an
    ase Atoms object (with constraints, where applicable).

    When making new entries for this dictionary, we recommend "pointing"
    the adsorbate upwards in the z-direction.
    '''
    # Initialize the adsorbate, and then add a blank entry so that we can relax empty
    # adslab systems, which are required for adsorption energy calculations.
    adsorbates = {}
    adsorbates[''] = Atoms()

    ''' Monatomics '''
    # We use uranium as a place-holder in our Local db of enumerated adsorption sites.
    adsorbates['U'] = Atoms('U')
    # We put the hydrogen half an angstrom below the origin to help in adsorb onto the
    # surface
    adsorbates['H'] = Atoms('H', positions=[[0., 0., -0.5]])
    adsorbates['O'] = Atoms('O')
    adsorbates['C'] = Atoms('C')

    ''' Diatomics '''
    # For diatomics (and above), it's a good practice to manually relax the gases
    # and then see how far apart they are. Then put first atom at the origin, and
    # put the second atom directly above it.
    adsorbates['CO'] = Atoms('CO', positions=[[0., 0., 0.],
                                              [0., 0., 1.2]])
    adsorbates['OH'] = Atoms('OH', positions=[[0., 0., 0.],
                                              [0., 0., 0.96]])

    ''' Triatomics '''
    # For OOH, we've found that most of our relaxations resulted in dissociation
    # of at least the hydrogen. As such, we put some hookean springs between
    # the atoms to keep the adsorbate together.
    ooh = Atoms('OOH', positions=[[0., 0., 0.],
                                  [0., 0., 1.55],
                                  [0, 0.94, 1.80]])
    ooh.set_constraint([ase.constraints.Hookean(a1=0, a2=1, rt=1.6, k=10.),   # Bind OO
                        ase.constraints.Hookean(a1=1, a2=2, rt=1.37, k=5.)])  # Bind OH
    adsorbates['OOH'] = ooh

    # All done!
    return adsorbates


def adsorption_parameters(adsorbate,
                          adsorption_site=None,
                          slabrepeat='(1, 1)',
                          num_slab_atoms=0,
                          settings='beef-vdw'):
    '''
    Generate some default parameters for an adsorption configuration and expected
    relaxation settings

    Inputs:
        adsorbate       If this is a string, this function will try to find it in the
                        default dictionary of adsorbates. If it is not in the default
                        dictionary, then this function will assume that you have passed
                        it an ase.Atoms object and then act accordingly.
        adsorption_site The cartesian coordinates of the binding atom in the adsorbate
        slabrepeat      The number of times the basic slab has been repeated
        numb_slab_atoms The number of atoms in the slab. We use this number to help
                        differentiate slab and adsorbate atoms (later on).
        settings        A string that Vaspy can use to create vasp settings.
                        Or `rpbe` if we want to use that
    '''
    # calc_settings returns a default set of calculational settings, but only if
    # the `settings` argument is a string.
    if isinstance(settings, str):
        settings = calc_settings(settings)

    # Use EAFP to figure out if the adsorbate that the user passed is in the
    # dictionary of default adsorbates, or if the user supplied an atoms object
    try:
        atoms = adsorbates_dict()[adsorbate]
        name = adsorbate
    except TypeError:
        atoms = adsorbate
        name = adsorbate.get_chemical_formula()

    return OrderedDict(numtosubmit=2,
                       min_xy=4.5,
                       relaxed=True,
                       num_slab_atoms=num_slab_atoms,
                       slabrepeat=slabrepeat,
                       adsorbates=[OrderedDict(name=name,
                                               atoms=pickle.dumps(atoms).encode('hex'),
                                               adsorption_site=adsorption_site)],
                       vasp_settings=OrderedDict(ibrion=2,
                                                 nsw=200,
                                                 isif=0,
                                                 isym=0,
                                                 kpts=[4, 4, 1],
                                                 lreal='Auto',
                                                 ediffg=-0.03,
                                                 symprec=1e-10,
                                                 **settings))
