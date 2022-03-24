from dbmanager import DBManager, DBManagerSQLite
from resultsmanager import ResultsManager
import matplotlib.pyplot as plt
import numpy as np
from rdkit import Chem
from rdkit.Geometry import Point3D
from rdkit.Chem import AllChem
import json

class VSManager():
    """Manager for coordinating different actions on virtual screening i.e. adding results to db, filtering, output options
    
    Attributes:
        dbman (DBManager): Interface module with database
        eworst (float): The worst scoring energy filter value requested by user
        filter_file (string): Name of file containing filters provided by user
        filtered_results (DB cursor object): Cursor object containing results passing requested filters (iterable)
        filters (dictionary): Dictionary containing user-specified filters
        no_print_flag (boolean): Flag specifying whether passing results should be printed to terminal
        out_opts (dictionary): Specified output options including data fields to output, export_poses_path, log file name
        output_manager (Outputter object): Manager for output tasks of log-writting, plotting, ligand SDF writing
        results_filters_list (List): List of tuples of filter option and value
        results_man (ResultsManager object): Manager for processing result files for insertion into database
    """
    def __init__(self, db_opts, rman_opts, filters, out_opts, filter_fname=None):
        """Initialize VSManager object. Will create DBManager object to serve as interface with database (currently implemented in SQLite). Will create ResultsManager to process result files. Will create Outputter object to assist in creating output files.
        
        Args:
            db_opts (dictionary): dictionary of options required by DBManager
            rman_opts (dictionary): dictionary of options required by results manager
            filters (dictionary): Dictionary containing user-specified filters
            out_opts (dictionary): Specified output options including data fields to output, export_poses_path, log file name
            filter_fname (None/string, optional): Name of file containing filters provided by user. None by default
        """
        self.filters = filters
        self.out_opts = out_opts
        self.eworst = self.filters['properties']['eworst'] # has default -3 kcal/mol
        self.filter_file = filter_fname
        self.no_print_flag = self.out_opts["no_print"]

        self.dbman = DBManagerSQLite(db_opts)
        self.results_man = ResultsManager(mode=rman_opts['mode'], dbman = self.dbman, chunk_size=rman_opts['chunk_size'], filelist=rman_opts['filelist'], numclusters=rman_opts['num_clusters'], no_print_flag = self.no_print_flag)
        self.output_manager = Outputter(self, self.out_opts['log'])

        #if requested, write database or add results to an existing one
        if self.dbman.write_db_flag or db_opts["add_results"]:
            print("Adding results...")
            self.add_results()

    def add_results(self):
        """
        Call results manager to process result files and add to database
        """
        self.results_man.process_results()

    def filter(self):
        """
        Prepare list of filters, then hand it off to DBManager to perform filtering. Create log of passing results.
        """

        #prepare list of filter values and keys for DBManager
        self.prepare_results_filter_list()

        print("Filtering results")
        #make sure we have ligand filter list
        if not self.filters['filter_ligands_flag']:
            self.filters["ligand_filters"] = []
        #ask DBManager to fetch results
        self.filtered_results = self.dbman.filter_results(self.results_filters_list, self.filters["ligand_filters"], self.out_opts['outfields'])
        number_passing_ligands = self.dbman.get_number_passing_ligands()[0]
        self.output_manager.log_num_passing_ligands(number_passing_ligands)
        for line in self.filtered_results:
            if not self.no_print_flag:
                print(line)
            self.output_manager.write_log_line(str(line).replace("(","").replace(")", ""))#strip parens from line, which is natively a tuple

    def plot(self):
        """
        Get data needed for creating Ligand Efficiency vs Energy scatter plot from DBManager. Call Outputter to create plot.
        """
        print("Creating plot of results")
        #get data from DBMan
        all_data, passing_data = self.dbman.get_plot_data()
        all_plot_data_binned = {}
        #bin the all_ligands data by 1000ths to make plotting faster
        for line in all_data:
            #add to dictionary as bin of energy and le
            data_bin = (round(line[0], 3), round(line[1],3))
            if data_bin not in all_plot_data_binned:
                all_plot_data_binned[data_bin] = 1
            else:
                all_plot_data_binned[data_bin] += 1
        #plot the data
        self.output_manager.plot_all_data(all_plot_data_binned)
        for line in passing_data:
            self.output_manager.plot_single_point(line[0],line[1],"red") #energy (line[0]) on x axis, le (line[1]) on y axis
        self.output_manager.save_scatterplot()

    def prepare_results_filter_list(self):
        """takes filters dictionary from option parser. Output list of tuples to be inserted into sql call string
        """

        filters_list = []

        #get property filters
        properties_keys = ['eworst',
        'ebest',
        'leworst',
        'lebest']

        property_filters = self.filters['properties']
        for key in properties_keys:
            if property_filters[key] != None:
                filters_list.append((key, property_filters[key]))

        #get interaction filters
        interaction_keys = ['V',
        'H',
        'R']

        interaction_filters = self.filters['interactions']
        for key in interaction_filters:
            if interaction_filters[key] != None:
                filters_list.append((key, interaction_filters[key]))

        #get interaction count filters
        interact_count_filters = self.filters["interactions_count"]
        for count in interact_count_filters:
            filters_list.append(count) #already a tuple, don't need to reformat

        #add react_any flag
        filters_list.append(("react_any",self.filters["react_any"]))

        self.results_filters_list = filters_list

    def write_molecule_sdfs(self):
        """have output manager write sdf molecules for passing results
        """
        passing_molecule_info = self.dbman.fetch_passing_ligand_output_info()
        for (ligname, smiles, atom_indices, h_parent_line) in passing_molecule_info:
            #create rdkit molecule
            mol = self.output_manager.create_molecule(ligname, smiles)
            #fetch coordinates for passing poses and add to rdkit mol
            passing_coordinates = self.dbman.fetch_passing_pose_coordinates(ligname)
            for ligand_pose, flexres_pose, flexres_names in passing_coordinates:
                mol = self.output_manager.add_pose_to_mol(mol, ligand_pose, atom_indices, flexres_pose, flexres_names, h_parent_line)
            #fetch coordinates for non-passing poses and add to mol
            nonpassing_coordinates = self.dbman.fetch_nonpassing_pose_coordinate(ligname)
            for pose in nonpassing_coordinates:
                mol = self.output_manager.add_pose_to_mol(mol, pose[0], atom_indices)

            #write out molecule
            self.output_manager.write_out_mol(ligname, mol)

    def close_database(self):
        """Tell database we are done and it can close the connection
        """
        self.dbman.close_connection()

###################################################
############ plotting class #######################
###################################################

class Outputter():

    """Class for creating outputs
    
    Attributes:
        ad_to_std_atomtypes (dictionary): Mapping of autodock atomtypes (key) to standard PDB atomtypes (value). Initialized as None, will be loaded from AD_to_STD_ATOMTYPES.json if required
        ax (pyplot axis): Axis for scatterplot of LE vs Energy
        fig_base_name (str): Name for figures excluding file extension
        flex_residue_smiles (Dictionary): Contains smiles used to create RDKit objects for flexible residues
        log (string): name for log file
        vsman (VSManager): VSManager object that created outputter
    
    """
    
    def __init__(self, vsman, log_file):
        """Initialize Outputter object and create log file
        
        Args:
            vsman (VSManager): VSManager object that created outputter
            log_file (string): name for log file
        """
        #flexible residue smiles with atom indices corresponding to flexres heteroatoms in pdbqt
        self.flex_residue_smiles = {"LYS":'CCCCCN',
                                    "CYS":'CCS',
                                    "TYR":'CC(c4c1).c24.c13.c2c3O',
                                    "SER":'CCO',
                                    "ARG":'CCCCN=C(N)N',
                                    "HIP":'CCC1([NH+]=CNC=1)',
                                    "VAL":'CC(C)C',
                                    "ASH":'CCC(=O)O',
                                    "GLH":'CCCC(=O)O',
                                    "HIE":'CCC1(N=CNC=1)',
                                    "GLU":'CCCC(=O)[O-]',
                                    "LEU":'CCC(C)C',
                                    "PHE":'CC(c4c1).c24.c13.c2c3',
                                    "GLN":'CCCC(N)=O',
                                    "ILE":'CC(C)CC',
                                    "MET":'CCCSC',
                                    "ASN":'CCC(=O)N',
                                    "ASP":'CCC(=O)O',
                                    "HID":'CCC1(NC=NC=1)',
                                    "THR":'CC(C)O',
                                    "TRP":'C1=CC=C2C(=C1)C(=CN2)CC'
                                    }

        self.ad_to_std_atomtypes = None

        self.log = log_file
        self.vsman = vsman

        if self.vsman.filter_file != None:
            self.fig_base_name = self.vsman.filter_file.split(".")[0]
        else:
            self.fig_base_name = "all_ligands"

        self._create_log_file()

    def plot_all_data(self, binned_data):
        """takes dictionary of binned data where key is the coordinates of the bin and value is the number of points in that bin. Adds to scatter plot colored by value
        
        Args:
            binned_data (dictionary): Keys are tuples of key and y value for bin. Value is the count of points falling into that bin.
        """
        #gather data
        energies = []
        leffs = []
        bin_counts = []
        for data_bin in binned_data.keys():
            energies.append(data_bin[0])
            leffs.append(data_bin[1])
            bin_counts.append(binned_data[data_bin])

        # start with a square Figure
        fig = plt.figure()

        gs = fig.add_gridspec(2, 2,  width_ratios=(7, 2), height_ratios=(2, 7),
                      left=0.1, right=0.9, bottom=0.1, top=0.9,
                      wspace=0.05, hspace=0.05)

        self.ax = fig.add_subplot(gs[1, 0])
        ax_histx = fig.add_subplot(gs[0, 0], sharex=self.ax)
        ax_histy = fig.add_subplot(gs[1, 1], sharey=self.ax)
        self.ax.set_xlabel("Best Binding Energy / kcal/mol")
        self.ax.set_ylabel("Best Ligand Efficiency")

        self.scatter_hist(energies, leffs, bin_counts, self.ax, ax_histx, ax_histy)


    def plot_single_point(self,x,y,color="black"):
        """Add point to scatter plot with given x and y coordinates and color.
        
        Args:
            x (float): x coordinate
            y (float): y coordinate
            color (str, optional): Color for point. Default black.
        """
        self.ax.scatter([x],[y],c=color)

    def save_scatterplot(self):
        """
        Saves current figure as [self.fig_base_name]_scatter.png
        """
        plt.savefig(self.fig_base_name + "_scatter.png", bbox_inches="tight")
        plt.close()

    def write_log_line(self, line):
        """write a single row to the log file
        
        Args:
            line (string): Line to write to log
        """
        with open(self.log, "a") as f:
          f.write(line)
          f.write("\n")

    def log_num_passing_ligands(self, number_passing_ligands):
        """
        Write the number of ligands which pass given filter to log file
        
        Args:
            number_passing_ligands (int): number of ligands that passed filter
        """
        with open(self.log, "a") as f:
            f.write("\n")
            f.write("Number passing ligands: {num} \n".format(num=number_passing_ligands))
            f.write("-----------------\n")

    def _clean_db_string(self, input_str):
        """take a db string representing a list, strips unwanted characters []'" 
        
        Args:
            input_str (str)
        
        Returns:
            String: cleaned string
        """
        return input_str.replace("[","").replace("]","").replace("'","").replace('"','')

    def replace_pdbqt_atomtypes(self, pdbqt_line):
        """replaces autodock-specific atomtypes with general ones. Reads AD-> general atomtype mapping from AD_to_STD_ATOMTYPES.json
        
        Args:
            pdbqt_line (string): Line representing an atom in pdbqt format
        
        Returns:
            String: pdbqt_line with atomtype replaced with general atomtypes recognized by RDKit
        
        Raises:
            RuntimeError: Will raise error if atomtype is not in AD_to_STD_ATOMTYPES.json
        """
        old_atomtype = pdbqt_line.split()[-1]
        
        #load autodock to standard atomtype dict if not loaded 
        if self.ad_to_std_atomtypes == None:
            with open('AD_to_STD_ATOMTYPES.json', 'r') as f:
                self.ad_to_std_atomtypes = json.load(f)

        #fetch new atomtype
        try:
            new_atomtype = self.ad_to_std_atomtypes[old_atomtype]
        except KeyError:
            raise RuntimeError("ERROR! Unrecognized atomtype {at} in flexible residue pdbqt!".format(at = old_atomtype))

        return pdbqt_line.replace(old_atomtype, new_atomtype)

    def create_molecule(self, ligname, smiles):
        """creates rdkit molecule from given ligand information
        
        Args:
            ligname (TYPE): Description
            smiles (TYPE): Description
        
        Returns:
            TYPE: Description
        
        Raises:
            RuntimeError: Description
        """

        if smiles == "":
            raise RuntimeError("Need SMILES for {molname}".format(molname=ligname))
        return Chem.MolFromSmiles(smiles)

    def add_pose_to_mol(self, mol, ligand_coordinates, index_map, flexres_lines, flexres_names, h_parent_line):
        """add given coordinates to given molecule as new conformer. Index_map maps order of coordinates to order in smile string used to generate rdkit mol
        
        Args:
            mol (RDKit Mol): RDKit molecule to add pose to
            ligand_coordinates (String): Ligand coordinate string from database. Will be split and cleaned
            index_map (string): String from database of index mapping from PDBQT atom order to RDKit molecule. Will be split and cleaned
            flexres_lines (String): String from database of PDBQT lines for flexible residues. Will be split and cleaned.
            flexres_names (string): String from database of residue names for flexible residues.
            h_parent_line (string): String from database of hydrogen atoms and their associated heavy atom.
        
        Returns:
            RDKit Mol: RDKit molecule object
        
        Raises:
            RuntimeError: Will raise error if number of coordinates does not match the number of atoms there are coordinates for.
        
        """
        n_atoms = mol.GetNumAtoms()
        conf = Chem.Conformer(n_atoms)
        #split ligand coordinate string from database into list, with each element containing the x,y,and z coordinates for one atom
        atom_coordinates = ligand_coordinates.split("],")
        index_map = index_map.split(",")
        if len(atom_coordinates) != n_atoms: #confirm we have the right number of coordinates
            raise RuntimeError("ERROR! Incorrect number of coordinates! Given {n_coords} coordinates for {n_at} atoms!".format(n_coords = len(atom_coordinates), n_at = n_atoms))
        for i in range(n_atoms):
            pdbqt_index = self._clean_db_string(index_map[i+1]) - 1
            x, y, z = [float(self._clean_db_string(coord)) for coord in atom_coordinates.split(",")]
            conf.SetAtomPosition(i, Point3D(x, y, z))
        conf_id = mol.AddConformer(conf)
        #Add hydrogens and correct their positions to match pdbqt
        mol = Chem.AddHs(mol, addCoords=True)
        conf = mol.GetConformer()
        used_h = []
        h_parent = self._format_h_parents(h_parent_line)
        for (parent_rdkit_index, h_pdbqt_index) in h_parents:
            h_pdbqt_index -= 1
            x, y, z = self._positions[self._current_pose][h_pdbqt_index, :]
            parent_atom = mol.GetAtomWithIdx(parent_rdkit_index - 1)
            candidate_hydrogens = [atom.GetIdx() for atom in parent_atom.GetNeighbors() if atom.GetAtomicNum() == 1]
            for h_rdkit_index in candidate_hydrogens:
                if h_rdkit_index not in used_h:
                    break
            used_h.append(h_rdkit_index)
            conf.SetAtomPosition(h_rdkit_index, Point3D(x, y, z))

        #make flexres rdkit molecules, add to our ligand molecule
        flexible_residues = self._clean_db_string(flexres_names).split(",")
        for i in range(len(flexible_residues)):
            #get the residue smiles string and pdbqt we need to make the required rdkit molecules
            res_smile = self.flex_residue_smiles[flexible_residues[i]]
            flexres_pdbqt = "\n".join(list(filter(None,[self.replace_pdbqt_atomtypes(line) for line in self._clean_db_string(flexres_coordinates.split("],")[i]).split(",")])))

            #make rdkit molecules and use template to ensure correct bond order
            template = AllChem.MolFromSmiles(res_smile)
            res_mol = AllChem.MolFromPDBBlock(flexres_pdbqt)
            res_mol = AllChem.AssignBondOrdersFromTemplate(template, res_mol)

            #combine with existing ligand and previous flexible residues
            mol = Chem.CombineMols(res_mol, mol)

        return mol

    def _format_h_parents(self, h_parent_line):
        """takes list of h_parent indices from database, formats into list of tuples
        
        Args:
            h_parent_line (String): H_parent remark line from database. Will clean and reformat.
        
        Returns:
            List: List of tuples (heavy_atom_index, H index)
        
        Raises:
            RuntimeError: Raise error if there is an odd number of indicies (not paired indices for hydrogen and parent)
        """

        h_parents = []
        integers = [int(self._clean_db_string(integer)) for integer in h_parent_line.split(",")]
        if len(integers) % 2 == 1:
            raise RuntimeError("Number of indices in H PARENT is odd")
        for j in range(int(len(integers) / 2)): 
            h_parents.append((integers[j*2], integers[j*2 + 1]))

        return h_parents

    def write_out_mol(self, ligname, mol):
        """writes out given mol as sdf
        
        Args:
            ligname (string): name of ligand that will be used to name output SDF file
            mol (RDKit Mol): RDKit molecule object to be written to SDF
        """
        filename = self.vsman.out_opts["export_poses_path"] + ligname.replace(".pdbqt", ".sdf")
        with SDWriter(filename) as w:
            w.write(mol)


    def _create_log_file(self):
        """
        Initializes log file
        """
        with open(self.log, 'w') as f:
            f.write("Filtered poses:\n")
            f.write("***************\n")

    def scatter_hist(self, x, y, z, ax, ax_histx, ax_histy):
        """
        Makes scatterplot with a histogram on each axis
        
        Args:
            x (list): x coordinates for data
            y (list): y coordinates for data
            z (list): z coordinates for data
            ax (matplotlib axis): scatterplot axis
            ax_histx (matplotlib axis): x histogram axis
            ax_histy (matplotlib axis): y histogram axis
        """
        # no labels
        ax_histx.tick_params(axis="x", labelbottom=False)
        ax_histy.tick_params(axis="y", labelleft=False)

        # the scatter plot:
        ax.scatter(x, y, c=z, cmap = "Blues")

        # now determine nice limits by hand:
        xbinwidth = 0.25
        ybinwidth = 0.01
        xminlim = (int(min(x)/xbinwidth) + 3) * xbinwidth
        xmaxlim = (int(max(x)/xbinwidth) + 3) * xbinwidth
        yminlim = (int(min(y)/ybinwidth) + 3) * ybinwidth
        ymaxlim = (int(max(y)/ybinwidth) + 3) * ybinwidth

        xbins = np.arange(xminlim, xmaxlim + xbinwidth, xbinwidth)
        ybins = np.arange(yminlim, ymaxlim + ybinwidth, ybinwidth)

        ax_histx.hist(x, bins=xbins)
        ax_histy.hist(y, bins=ybins, orientation='horizontal')