# @file distgeom.py
#  Implements a basic distance geometry conformer search routine
#
#  Written by Terry Gani for HJK Group
#  Modified for improved support of bidentates on 07/08/2019 by Daniel Harper
#
#  Dpt of Chemical Engineering, MIT
#
#  Adapted from:
#
#  [1] J. M. Blaney and J. S. Dixon, "Distance Geometry in Molecular Modeling", in Reviews in Computational Chemistry, VCH (1994)
#
#  [2] G. Crippen and T. F. Havel, "Distance Geometry and Molecular Conformation", in Chemometrics Research Studies Series, Wiley (1988)

import numpy as np
import numpy
import openbabel
from scipy import optimize
from math import sqrt, cos

from molSimplify.Classes.atom3D import atom3D
from molSimplify.Classes.mol3D import mol3D
from molSimplify.Classes.globalvars import (vdwrad)
from molSimplify.Scripts.geometry import (distance,
                                          norm,
                                          vecangle,
                                          vecdiff)
from molSimplify.Scripts.molSimplify_io import (lig_load,
                                                loadcoord)


# Applies the cosine rule to get the length of AC given lengths of AB, BC and angle ABC
#  @param AB Length of AB
#  @param BC Length of BC
#  @param theta Angle in degrees
#  @return Length of AC

def CosRule(AB, BC, theta):
    theta = np.pi*theta/180
    AC = sqrt(AB**2+BC**2-2*AB*BC*cos(theta))
    return AC

# Apply the cosine rule to find the angle ABC given points A,B, and C
#  @param A the coordinates of A
#  @param B the coordinates of B
#  @param C the coordinates of C
#  @return theta The angle ABC in degrees

def inverseCosRule(A, B, C):
    BA = np.linalg.norm(np.array(A)-np.array(B))
    BC = np.linalg.norm(np.array(C)-np.array(B))
    AC = np.linalg.norm(np.array(C)-np.array(A))
    theta = np.arccos((BA**2+BC**2-AC**2)/(2*BA*BC))
    return np.rad2deg(theta)

# Generate distance bounds matrices
#
#  The basic idea is outlined in ref [1].
#
#  We first apply 1-2 (bond length) and 1-3 (bond angle) constraints, read from the FF-optimized initial conformer.
#
#  Next, to bias the search towards coordinating conformers, approximate connection atom distance constraints based on topological distances are also included.
#  @param mol mol3D of molecule
#  @param natoms Number of atoms in molecule
#  @param catoms List of ligand connection atoms (default empty)
#  @param A Distance 2 connectivity matrix
#  @return Lower and upper bounds matrices

def GetBoundsMatrices(mol, natoms, catoms=[], shape=[], A=[]):
    LB = np.zeros((natoms, natoms))  # lower bound
    UB = np.zeros((natoms, natoms))  # upper bound, both symmetric
    # Set constraints for all atoms excluding the dummy metal atom
    for i in range(natoms-1):
        for j in range(natoms-1):
            # 1-2 constraints: UB = LB = BL
            if mol.OBMol.GetBond(i+1, j+1) is not None:
                UB[i][j] = distance(mol.getAtomCoords(i), mol.getAtomCoords(j))
                UB[j][i] = distance(mol.getAtomCoords(i), mol.getAtomCoords(j))
                LB[i][j] = distance(mol.getAtomCoords(i), mol.getAtomCoords(j))
                LB[j][i] = distance(mol.getAtomCoords(i), mol.getAtomCoords(j))
    for i in range(natoms-1):
        for j in range(natoms-1):
            for k in range(natoms-1):
                # 1-3 constraints: UB = LB = BL
                if mol.OBMol.GetBond(i+1, j+1) is not None and mol.OBMol.GetBond(j+1, k+1) is not None and j != k and i != k:
                    AB = vecdiff(mol.getAtomCoords(j), mol.getAtomCoords(i))
                    BC = vecdiff(mol.getAtomCoords(k), mol.getAtomCoords(j))
                    UB[i][k] = CosRule(norm(AB), norm(BC),
                                       180-vecangle(AB, BC))
                    UB[k][i] = CosRule(norm(AB), norm(BC),
                                       180-vecangle(AB, BC))
                    LB[i][k] = CosRule(norm(AB), norm(BC),
                                       180-vecangle(AB, BC))
                    LB[k][i] = CosRule(norm(AB), norm(BC),
                                       180-vecangle(AB, BC))

    # Set constraints for atoms bonded to the dummy metal atom
    # Currently assumes all M-L bonds are 2 Angstroms
    dummy_idx = natoms-1
    M_L_bond = 2
    for catom in catoms:
        # Set 1-2 constraints
        UB[catom][dummy_idx] = M_L_bond
        UB[dummy_idx][catom] = M_L_bond
        LB[catom][dummy_idx] = M_L_bond
        LB[dummy_idx][catom] = M_L_bond
    if len(catoms) > 1:
        # Set 1-3 contraints for ligating atoms
        for i in range(len(catoms[:-1])):
            for j in range(i+1, len(catoms)):
                angle = shape[str(i)+'-'+str(j)]
                lig_distance = CosRule(M_L_bond, M_L_bond, angle)
                UB[catoms[i]][catoms[j]] = lig_distance
                UB[catoms[j]][catoms[i]] = lig_distance
                LB[catoms[i]][catoms[j]] = lig_distance
                LB[catoms[j]][catoms[i]] = lig_distance

    expanded_vdwrad = vdwrad.copy()
    expanded_vdwrad['Fe'] = 1.5  # Default vdw radius for the dummy metal is 1.5
    for i in range(natoms):
        for j in range(i):
            # fill LBs with sums of vdW radii and UBs with arbitrary large cutoff
            if LB[i][j] == 0:
                LB[i][j] = expanded_vdwrad[mol.getAtom(
                    i).sym] + expanded_vdwrad[mol.getAtom(j).sym]
                LB[j][i] = expanded_vdwrad[mol.getAtom(
                    i).sym] + expanded_vdwrad[mol.getAtom(j).sym]
                UB[i][j] = 100
                UB[j][i] = 100
    return LB, UB

# Triangle inequality bounds smoothing
#
#  Copied from ref [2], pp. 252-253
#
#  Scales O(N^3).
#  @param LB Lower bounds matrix
#  @param UB Upper bounds matrix
#  @param natoms Number of atoms in molecule
#  @return Triangularized bounds matrices

def Triangle(LB, UB, natoms):
    LL = LB
    UL = UB
    for k in range(natoms):
        for i in range(natoms-1):
            for j in range(i, natoms):
                if UL[i][j] > UL[i][k] + UL[k][j]:
                    UL[i][j] = UL[i][k] + UL[k][j]
                    UL[j][i] = UL[i][k] + UL[k][j]
                if LL[i][j] < LL[i][k] - UL[k][j]:
                    LL[i][j] = LL[i][k] - UL[k][j]
                    LL[j][i] = LL[i][k] - UL[k][j]
                else:
                    if LL[i][j] < LL[j][k] - UL[k][i]:
                        LL[i][j] = LL[j][k] - UL[k][i]
                        LL[j][i] = LL[j][k] - UL[k][i]
    return LL, UL

# Metrization to select random in-range distances
#
#  Copied from ref [2], pp. 253-254
#  @param LB Lower bounds matrix
#  @param UB Upper bounds matrix
#  @param natoms Number of atoms in molecule
#  @param Full Full metrization (scales O(N^5), default false)
#  @param seed Random number seed (default none)
#  @return Distance matrix

def Metrize(LB, UB, natoms, Full=False, seed=False):
    if seed:
        numpy.random.seed(seed)
    D = np.zeros((natoms, natoms))
    LB, UB = Triangle(LB, UB, natoms)
    #First generate a random distance for all atom pairings not involving the metal
    for i in range(natoms-1):
        for j in range(i, natoms-1):
            # ~ if Full:
                # ~ LB, UB = Triangle(LB, UB, natoms)
            if UB[i][j] < LB[i][j]:  # ensure that the upper bound is larger than the lower bound
                UB[i][j] = LB[i][j]
            D[i][j] = np.random.uniform(LB[i][j], UB[i][j])
            D[j][i] = D[i][j]
    
    #For pairs involving the metal, set the distance to 100 Angstroms regardless of the triangle rule
    #This encourages the algorithm to select conformations which don't crowd the metal, as these often lead to failure
    for j in range(natoms):
        if UB[natoms-1][j] < LB[natoms-1][j]:  # ensure that the upper bound is larger than the lower bound
            UB[natoms-1][j] = LB[natoms-1][j]
        D[natoms-1][j] = 100
        D[j][natoms-1] = D[natoms-1][j]
    return D

# Get distances of each atom to CM given the distance matrix
# CM = Center mass???
#
#  Copied from ref [2], pp. 309
#  @param D Distance matrix
#  @param natoms Number of atoms in molecule
#  @return Vector of CM distances, flag for successful search

def GetCMDists(D, natoms):
    D0 = np.zeros(natoms)
    status = True
    for i in range(natoms):
        for j in range(natoms):
            D0[i] += D[i][j]**2/natoms
        for j in range(natoms):
            for k in range(j, natoms):
                D0[i] -= (D[j][k])**2/natoms**2
        D0[i] = sqrt(D0[i])
    return D0, status

# Get metric matrix from distance matrix and CM distances
#
#  Copied from ref [1], pp. 306
#  @param D Distance matrix
#  @param D0 Vector of CM distances
#  @param natoms Number of atoms in molecule
#  @return Metric matrix

def GetMetricMatrix(D, D0, natoms):
    G = np.zeros((natoms, natoms))
    for i in range(natoms):
        for j in range(natoms):
            G[i][j] = (D0[i]**2 + D0[j]**2 - D[i][j]**2)/2
    return G

# Gets 3 largest eigenvalues and corresponding eigenvectors of metric matrix
#  @param G Metric matrix
#  @param natoms Number of atoms in molecule
#  @return Three largest eigenvalues and corresponding eigenvectors

def Get3Eigs(G, natoms):
    L = np.zeros((3, 3))
    V = np.zeros((natoms, 3))
    l, v = np.linalg.eigh(G)
    for i in [0, 1, 2]:
        #print('natoms is '+ str(natoms))
        #print('l is '+ str(l))
        L[i][i] = sqrt(max(l[natoms-1-i], 0))
        V[:, i] = v[:, natoms-1-i]
    return L, V

# Computes distance error function for scipy optimization
#
#  Copied from E3 in pp. 311 of ref. [1]
#  @param x 1D array of coordinates to be optimized
#  @param *args Other parameters (refer to scipy.optimize docs)
#  @return Objective function

def DistErr(x, *args):
    E = 0
    LB, UB, natoms = args
    for i in range(natoms-1):
        for j in range(i+1, natoms):
            ri = [x[3*i], x[3*i+1], x[3*i+2]]
            rj = [x[3*j], x[3*j+1], x[3*j+2]]
            dij = distance(ri, rj)
            uij = UB[i][j]
            lij = LB[i][j]
            E += (dij**2/(uij**2) - 1)**2
            E += (2*lij**2/(lij**2 + dij**2) - 1)**2
    return np.asarray(E)

# Computes gradient of distance error function for scipy optimization
#
#  Copied from E3 in pp. 311 of ref. [1]
#  @param x 1D array of coordinates to be optimized
#  @param *args Other parameters (refer to scipy.optimize docs)
#  @return Objective function gradient

def DistErrGrad(x, *args):
    LB, UB, natoms = args
    g = np.zeros(3*natoms)
    for i in range(natoms):
        jr = list(range(natoms))
        jr.remove(i)
        for j in jr:
            ri = [x[3*i], x[3*i+1], x[3*i+2]]
            rj = [x[3*j], x[3*j+1], x[3*j+2]]
            dij = distance(ri, rj)
            uij = UB[i][j]
            lij = LB[i][j]
            g[3*i] += (4*((dij/uij)**2-1)/(uij**2) - (8/lij**2)*(2*(lij**2 /
                                                                    (lij**2+dij**2))-1)/((1+(dij/lij)**2)**2))*(x[3*i]-x[3*j])  # xi
            g[3*i+1] += (4*((dij/uij)**2-1)/(uij**2) - (8/lij**2)*(2*(lij**2 /
                                                                      (lij**2+dij**2))-1)/((1+(dij/lij)**2)**2))*(x[3*i+1]-x[3*j+1])  # yi
            g[3*i+2] += (4*((dij/uij)**2-1)/(uij**2) - (8/lij**2)*(2*(lij**2 /
                                                                      (lij**2+dij**2))-1)/((1+(dij/lij)**2)**2))*(x[3*i+2]-x[3*j+2])  # zi
    return g

# Further cleans up with OB FF and saves to a new mol3D object
#
#  Note that distance geometry tends to produce puckered aromatic rings because of the lack of explicit impropers, see Riniker et al. JCIM (2015) 55, 2562-74 for details.
#
#  Hence, a FF optimization (with connection atoms constrained) is recommended to clean up the structure.
#  @param X Array of coordinates
#  @param mol mol3D of original molecule
#  @param ffclean Flag for OB FF cleanup (default True)
#  @param catoms List of connection atoms (default empty), used to generate FF constraints if specified
#  @return mol3D of new conformer

def SaveConf(X, mol, ffclean=True, catoms=[]):
    conf3D = mol3D()
    conf3D.copymol3D(mol)
    # set coordinates using OBMol to keep bonding info
    OBMol = conf3D.OBMol
    for i, atom in enumerate(openbabel.OBMolAtomIter(OBMol)):
        atom.SetVector(X[i, 0], X[i, 1], X[i, 2])
    
    #First stage of cleaning takes place with the metal still present
    if ffclean:
        ff = openbabel.OBForceField.FindForceField('UFF')
        s = ff.Setup(OBMol)
        if not s:
            print('FF setup failed')
            
        for i in range(200):
            ff.SteepestDescent(10)
            ff.ConjugateGradients(10)
        ff.GetCoordinates(OBMol)

    last_atom_index = OBMol.NumAtoms() #Delete the dummy metal atom that we added earlier
    metal_atom = OBMol.GetAtom(last_atom_index)
    OBMol.DeleteAtom(metal_atom)
    
    #Second stage of cleaning removes the metal, but uses constraints on the bonding atoms to ensure a binding conformer is maintained
    #This stage is critical for getting planar aromatic ligands like porphyrin and correct. Not really sure why though...
    if ffclean:
        ff = openbabel.OBForceField.FindForceField('UFF')
        constr = openbabel.OBFFConstraints()
        for atom in catoms:
            constr.AddAtomConstraint(atom+1) 
        s = ff.Setup(OBMol,constr)
        if not s:
            print('FF setup failed')
            
        for i in range(200):
            ff.SteepestDescent(10)
            ff.ConjugateGradients(10)
        ff.GetCoordinates(OBMol)
    
    conf3D.OBMol = OBMol
    conf3D.convert2mol3D()
    return conf3D

# Determines the relative positioning of different ligating atoms
# @param args
# @return A dictionary of angles (in degrees)between catoms

def findshape(args, master_ligand):
    core = loadcoord(args.geometry)

    # load ligands and identify the denticity of each
    ligands = []
    for counter, i in enumerate(args.lig):
        ligands.append(lig_load(i)[0])
    number_of_smiles_ligands = 0
    for counter, lig in enumerate(ligands):
        if lig.ident == 'smi':
            ligands[counter].denticity = len(
                args.smicat[number_of_smiles_ligands])

    bind = 1
    for counter, i in enumerate(ligands):
        if i.name == master_ligand.name:
            master_denticity = i.denticity
            break
        else:
            bind += 1*int(args.ligocc[counter])*int(i.denticity)
    binding_locations = (np.array(list(range(master_denticity))))+bind

    metal_coords = np.array(core[0])
    ligating_coords = []
    for i in binding_locations:
        ligating_coords.append(np.array(core[i]))

    angles_dict = dict()
    for i in range(len(ligating_coords)):
        for j in range(len(ligating_coords)):
            angles_dict[str(i)+'-'+str(j)] = inverseCosRule(ligating_coords[i],
                                                            metal_coords, ligating_coords[j])
    return angles_dict

# Uses distance geometry to get a random conformer.
#  @param mol mol3D of molecule
#  @param catoms List of connection atoms (default empty), used to generate additional constraints if specified (see GetBoundsMatrices())
#  @return mol3D of new conformer

def GetConf(mol, args, catoms=[]):
    # Create a mol3D copy with a dummy metal metal
    Conf3D = mol3D()
    Conf3D.copymol3D(mol)
    Conf3D.addAtom(atom3D('Fe', [0, 0, 0])) #Add dummy metal to the mol3D
    dummy_metal = openbabel.OBAtom() #And add the dummy metal to the OBmol
    dummy_metal.SetAtomicNum(26)
    Conf3D.OBMol.AddAtom(dummy_metal)
    for i in catoms:
        Conf3D.OBMol.AddBond(i+1, Conf3D.OBMol.NumAtoms(), 1)
    natoms = Conf3D.natoms
    Conf3D.createMolecularGraph()

    shape = findshape(args, mol)
    LB, UB = GetBoundsMatrices(Conf3D, natoms, catoms, shape)
    status = False
    while not status:
        D = Metrize(LB, UB, natoms)
        D0, status = GetCMDists(D, natoms)
    G = GetMetricMatrix(D, D0, natoms)
    L, V = Get3Eigs(G, natoms)
    X = np.dot(V, L)  # get projection
    x = np.reshape(X, 3*natoms)
    res1 = optimize.fmin_cg(DistErr, x, fprime=DistErrGrad,
                            gtol=0.1, args=(LB, UB, natoms), disp=0)
    X = np.reshape(res1, (natoms, 3))
    Conf3D = SaveConf(X, Conf3D, True, catoms)

    return Conf3D

# for testing
#
#n4py
#molsimplify -core ru -lig 'n1ccccc1CN(Cc2ccccn2)C(c3ccccn3)c4ccccn4' water -ligocc 1 1 -smicat [1,15,22,28,8] -spin 1 -ligloc True -geometry oct -rprompt True -ffoption A
#heptacoordinate water oxidation catalyst
#molsimplify -core ru -lig 'n1c(C(=O)[O-])cccc1c2cccc(c3cccc(C(=O)[O-])n3)n2' water pyridine -ligocc 1 1 2 -smicat [1,24,23,22] -spin 1 -ligloc True -geometry pbp -ffoption A
#same water oxidation catalyst in a hexacoordinate binding pattern
#molsimplify -core ru -lig 'n1c(C(=O)[O-])cccc1c2cccc(c3cccc(C(=O)[O-])n3)n2' water pyridine -ligocc 1 1 2 -smicat [1,24,23] -spin 1 -ligloc True -geometry oct -ffoption A
#tetrahedral with 2 bidentates
#molsimplify -core fe -lig 'n1ccccc1c2ccccn2' 'CC(=O)C=C([O-])C' -ligocc 1 1 -smicat [[1,12],[3,6]] -ligloc True -geometry thd -ffoption A -rprompt True
#mol,emsg = lig_load('c1ccc(c(c1)C=NCCN=Cc2ccccc2[O-])[O-]')
#mol,emsg = lig_load('N(C)1CCN(C)CCCN(C)CCN(C)CCC1')
#catoms = [7,10,18,19]
#catoms = [0,4,9,13]
# mol.convert2mol3D()
#conf = GetConf(mol,catoms)
# conf.writexyz('conf')
