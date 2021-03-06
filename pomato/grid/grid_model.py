
import logging
import datetime as dt
import itertools
import numpy as np
import pandas as pd
import types
from pathlib import Path

import pomato
import pomato.tools as tools

class GridModel():
    """GridRepresentation of POMATO, represents the network in the market model.

    The GridRepresentation creates a grid representation to be used in the market model based on the
    chosen options. This module acts as a combinator of the data and grid modules
    and allow to easily change grid representation for the market model.

    Its main feature is a minimal nodal/zonal N-1 grid representation which is achieved through a
    redundancy removal algorithm based on the "Clarkson" algorithm.

    For a more comprehensive documentation note that the ptdf matrix, regardless if with/without
    contingencies or representing nodal/zonal sensitivities, is denoted as matrix A in this module and
    the line limits as vector b. Therefore the resulting power flow problem is simply written in the
    form of a linear problem Ax <= b. The redundancy reduction algorithm finds the smallest set of
    constraints, essential set, that fully defines the problem. The indices, i.e. the rows in A or
    cbco's, are called essential indices.

    The the class attributes are divided into core attributes, with *grid_representation* as the
    mein outout/result and additional attributes that are used to facilitate the redundancy removal
    algorithm.

    Parameters
    ----------
    wdir : pathlib.Path
        Working directory
    data : :class:`~pomato.data.DataManagement`
       An instance of the DataManagement class with processed input data.
    grid : :class:`~pomato.grid.GridTopology`
       An instance of the GridModel class.
    options : dict
        The options from POMATO main method persist in the CBCOModule.

    Attributes
    ----------
    wdir, julia_dir : pathlib.Path
        Working directory, Sub-directory for temporary files related with the
        redundancy removal algorithm.
    options : dict
        The options from DataManagement persist in the InputProcessing.
    grid : :class:`~pomato.grid.GridTopology`
        Instance of the GridTopology class. Provides functionality to create N-0 and (filtered) N-1 ptdf.
    data : :class:`~pomato.data.DataManagement`
       Instance of the DataManagement class with processed input data.
    grid_representation : types.SimpleNamespace
        Containing the grid representation to be used in the market model and the determination of
        the economic dispatch. Depends on the chosen configuration in the options file.
    julia_instance : :class:`~pomato.tools.JuliaDaemon`
        Julia process that is initialized when used the first time and then kept to be able to
        easily re-run the redundancy algorithm without restarting a julia process.
    """

    def __init__(self, wdir, grid, data, option):
        # Import Logger
        self.logger = logging.getLogger('Log.MarketModel.CBCOModule')
        self.logger.info("Initializing the CBCOModule....")

        self.options = option
        self.wdir = wdir
        self.package_dir = Path(pomato.__path__[0])


        self.julia_dir = wdir.joinpath("data_temp/julia_files")
        tools.create_folder_structure(self.wdir, self.logger)

        # Core attributes
        self.grid = grid
        self.data = data
        self.grid_representation = types.SimpleNamespace(option=None,
                                                         multiple_slack=None,
                                                         slack_zones=None,
                                                         grid=pd.DataFrame(),
                                                         redispatch_grid=pd.DataFrame(),
                                                         ntc=pd.DataFrame())
       
        self.julia_instance = None
        self.logger.info("CBCOModule Initialized!")

    def _start_julia_daemon(self):
        self.julia_instance = tools.JuliaDaemon(self.logger, self.wdir, self.package_dir, "redundancy_removal")

    def create_grid_representation(self):
        """Create grid representation based on model type.

        Options are: dispatch, ntc, nodal, zonal, cbco_nodal, cbco_zonal.

        *grid_representation* contains the following:
            - *option*: The chosen option of grid representation.
            - *multiple_slack*: Bool indicator if there are multiple slacks.
            - *slack_zones*: dict to map each node to a slack/reference node.
            - *grid*: DataFrame including the ptdf for each line/outage,
              depending on chosen option, including line capacities and
              regional information.
            - *redispatch_grid*: DataFrame including the ptdf for the redispatch.
              As default this is nodal but could also be an N-1 ptdf, similar to
              *grid*.
            - *ntc*: DataFrame with the zonal commercial exchange capacities.

        All values are set according the chosen option and might remain empty.
        """
        # Data Structure of grid_representation dict
        self.grid_representation.option = self.options["optimization"]["type"]
        self.grid_representation.multiple_slack = self.grid.multiple_slack
        self.grid_representation.slack_zones = self.grid.slack_zones()

        if self.options["optimization"]["type"] == "ntc":
            self.process_ntc()
        elif self.options["optimization"]["type"] == "nodal":
            self.process_nodal()
        elif self.options["optimization"]["type"] == "zonal":
            self.process_zonal()
        elif self.options["optimization"]["type"] == "cbco_nodal":
            self.process_cbco_nodal()
        elif self.options["optimization"]["type"] == "cbco_zonal":
            self.process_cbco_zonal()
        else:
            self.logger.info("No grid representation needed for dispatch model")

        if self.options["optimization"]["redispatch"]["include"]:
            self.add_redispatch_grid()

    def process_nodal(self):
        """Process grid information for nodal N-0 representation.

        Here *grid_representation.grid* consists of the N-0 ptdf.

        There is the option to try to reduce this ptdf, however the number of
        redundant constraints is expected to be very low.

        """
        grid_option = self.options["grid"]
        if grid_option["cbco_option"] == "nodal_clarkson":
            A = self.grid.ptdf
            b = self.grid.lines.maxflow.values.reshape(len(self.grid.lines.index), 1)
            info = pd.DataFrame(columns=self.grid.nodes.index, data=A)
            info["cb"] = list(self.grid.lines.index)
            info["co"] = ["basecase" for i in range(0, len(self.grid.lines.index))]
            info["ram"] = b
            info = info[["cb", "co", "ram"] + list(self.grid.nodes.index)]

            nodal_injection_limits = self.create_nodal_injection_limits()
            cbco_index = self.clarkson_algorithm(A=A, b=b, x_bounds=nodal_injection_limits)
            self.grid_representation.grid = self.return_cbco(info, cbco_index)
            self.grid_representation.grid.ram *= grid_option["capacity_multiplier"]

        else:
            ptdf_df = pd.DataFrame(index=self.grid.lines.index,
                                   columns=self.grid.nodes.index,
                                   data=self.grid.ptdf)
            ptdf_df["ram"] = self.grid.lines.maxflow*self.options["grid"]["capacity_multiplier"]
            self.grid_representation.grid = ptdf_df
            self.grid_representation.grid = self._add_zone_to_grid_representation(self.grid_representation.grid)
            
    def add_redispatch_grid(self):
        """Add nodal N-0 grid representation as redispatch grid.

        Here *grid_representation.redispatch_grid* consists of the N-0 ptdf.
        """
        ptdf_df = pd.DataFrame(index=self.grid.lines.index,
                               columns=self.grid.nodes.index,
                               data=self.grid.ptdf)
        ptdf_df["ram"] = self.grid.lines.maxflow*self.options["grid"]["capacity_multiplier"]
        self.grid_representation.redispatch_grid = ptdf_df
        self.grid_representation.redispatch_grid = self._add_zone_to_grid_representation(self.grid_representation.redispatch_grid)

    def process_zonal(self):
        """Process grid information for zonal N-0 representation.

        Calculates the zonal N-0 ptdf, based on the nodal N-0 ptdf with a
        generation shift key.

        There is the option to try to reduce this ptdf, however the number of
        redundant constraints is expected to be small.

        Since the zonal ptdf constraints the commercial exchange, a dummy ntc
        table is added to not allow unintuitive commercial flows.

        """
        gsk = self.options["grid"]["gsk"]
        grid_option = self.options["grid"]

        if grid_option["cbco_option"] == "clarkson":
            A = self.grid.ptdf
            # nodal -> zonal ptdf via gsk
            A = np.dot(A, self.create_gsk(gsk))
            b = self.grid.lines.maxflow.values.reshape(len(self.grid.lines.index), 1)
            info = pd.DataFrame(columns=self.data.zones.index, data=A)
            info["cb"] = list(self.grid.lines.index)
            info["co"] = ["basecase" for i in range(0, len(self.grid.lines.index))]
            info["ram"] = b
            info = info[["cb", "co", "ram"] + list(self.data.zones.index)]

            cbco_index = self.clarkson_algorithm(A=A, b=b)
            self.grid_representation.grid = self.return_cbco(info, cbco_index)
            self.grid_representation.grid.ram *= grid_option["capacity_multiplier"]

        else:
            ptdf = np.dot(self.grid.ptdf, self.create_gsk(gsk))
            ptdf_df = pd.DataFrame(index=self.grid.lines.index,
                                   columns=self.data.zones.index,
                                   data=np.round(ptdf, decimals=4))

            ptdf_df["ram"] = self.grid.lines.maxflow*self.options["grid"]["capacity_multiplier"]
            self.grid_representation.grid = ptdf_df
        self.create_ntc()

    def process_cbco_zonal(self):
        """Process grid information for zonal N-1 representation.

        Based on chosen sensitivity and GSK the return of
        :meth:`~pomato.cbco.create_cbco_data` runs the redundancy removal
        algorithm to reduce the number of constraints to a minimal set.

        The redundancy removal is very efficient for this type of grid
        representation as the dimensionality of the ptdf is the number of zones
        and therefore low.

        Since the zonal ptdf constraints the commercial exchange, a dummy ntc
        table is added to not allow unintuitive commercial flows.

        """
        grid_option = self.options["grid"]
        A, b, cbco_info = self.create_cbco_data(grid_option["sensitivity"],
                                                preprocess=True,
                                                gsk=grid_option["gsk"])

        cbco_index = self.clarkson_algorithm(A=A, b=b)

        self.grid_representation.grid = self.return_cbco(cbco_info, cbco_index)
        self.grid_representation.grid.ram *= grid_option["capacity_multiplier"]
        self.create_ntc()

    def _add_zone_to_grid_representation(self, grid):
        """Add information in which country a line is located.
        
        By adding two columns in dataframe: zone_i, zone_j. This information is needed for zonal redispatch 
        to identify which lines should be redispatched. 
        """

        if "cb" in grid.columns:
            grid["zone_i"] = self.grid.nodes.loc[self.grid.lines.loc[grid.cb, "node_i"], "zone"].values
            grid["zone_j"] = self.grid.nodes.loc[self.grid.lines.loc[grid.cb, "node_j"], "zone"].values
        else:
            grid["zone_i"] = self.grid.nodes.loc[self.grid.lines.loc[grid.index, "node_i"], "zone"].values
            grid["zone_j"] = self.grid.nodes.loc[self.grid.lines.loc[grid.index, "node_j"], "zone"].values

        return grid

    def process_cbco_nodal(self):
        """Process grid information for nodal N-1 representation.

        Based on chosen sensitivity and GSK the return of
        :meth:`~pomato.cbco.create_cbco_data` runs the redundancy removal
        algorithm to reduce the number of constraints to a minimal set. The
        redundancy removal algorithm can take long to conclude, e.g. about
        2 hours for the DE case study which comprises of ~450 nodes and ~1000
        lines.
        Therefore is useful to keep the resulting file with if the relevant
        cbco's and just read it in when needed. This is done by specifying the
        cbco fil in *options["grid"]["precalc_filename"]*.

        There are multiple options to pick, where one is the obvious best
        *clarkson_base*. This runs the redundancy removal algorithm including
        bounds on the nodal injections, which are calculated based on
        installed capacity and availability/load timeseries. The other options
        are: *clarkson* redundancy removal without bounds on nodal injections
        and "save" saving the relevant files for the RedundancyRemoval
        algorithm so that it can be run separately from the python POMATO.

        """
        A, b, cbco_info = self.create_cbco_data(self.options["grid"]["sensitivity"],
                                                self.options["grid"]["preprocess"])

        if self.options["grid"]["precalc_filename"]:
            try:
                filename = self.options["grid"]["precalc_filename"]
                self.logger.info("Using cbco indices from pre-calc: %s", filename)
                precalc_cbco = pd.read_csv(self.julia_dir.joinpath(f"cbco_data/{filename}.csv"),
                                           delimiter=',')
                if len(precalc_cbco.columns) > 1:
                    condition = cbco_info[["cb", "co"]].apply(tuple, axis=1) \
                                    .isin(precalc_cbco[["cb", "co"]].apply(tuple, axis=1))
                    cbco_index = list(cbco_info.reset_index().index[condition])
                    self.logger.info("Number of CBCOs from pre-calc: %s", str(len(cbco_index)))
                else:
                    cbco_index = list(precalc_cbco.constraints.values)
                    self.logger.info("Number of CBCOs from pre-calc: %s", str(len(cbco_index)))

            except FileNotFoundError:
                self.logger.warning("FileNotFound: No Precalc available")
                self.logger.warning("Running with full N-1 representation (subject to the lodf filter)")
                cbco_index = cbco_index = list(range(0, len(b)))

        else:
            # 3 valid args supported for cbco_option:
            # clarkson, clarkson_base, full (default)
            if self.options["grid"]["cbco_option"] == "full":
                cbco_index = list(range(0, len(b)))

            elif self.options["grid"]["cbco_option"] == "clarkson_base":
                nodal_injection_limits = self.create_nodal_injection_limits()
                cbco_index = self.clarkson_algorithm(A=A, b=b, x_bounds=nodal_injection_limits)

            elif self.options["grid"]["cbco_option"] == "clarkson":
                cbco_index = self.clarkson_algorithm(A=A, b=b)

            elif self.options["grid"]["cbco_option"] == "save":
                nodal_injection_limits = self.create_nodal_injection_limits()
                cbco_index = list(range(0, len(b)))

                self.write_cbco_info(self.julia_dir.joinpath("cbco_data"), "py_save", 
                                     A=A, b=b, Ab_info=cbco_info, x_bounds=nodal_injection_limits)
            else:
                self.logger.warning("No valid cbco_option set!")


        self.grid_representation.grid = self.return_cbco(cbco_info, cbco_index)
        self.grid_representation.grid = self._add_zone_to_grid_representation(self.grid_representation.grid)
        self.grid_representation.grid.ram *= self.options["grid"]["capacity_multiplier"]

    def create_cbco_data(self, sensitivity=5e-2, preprocess=True, gsk=None):
        """Create all relevant N-1 ptdfs in the form of Ax<b (ptdf x < ram).

        This uses the method :meth:`~pomato.grid.create_filtered_n_1_ptdf` to
        generate a filtered ptdf matrix, including outages with a higher impact
        of the argument *sensitivity*.

        Parameters
        ----------
        sensitivity : float, optional
            The sensitivity defines the threshold from which outages are
            considered critical. A outage that can impact the lineflow,
            relative to its maximum capacity, more than the sensitivity is
            considered critical.
        preprocess : bool, optional
            Performing a light preprocessing by removing duplicate constraints.
        gsk : np.ndarray, optional
            When gsk is an argument, this method creates a zonal ptdf matrix
            with it.

        Returns
        -------
        A : np.ndarray
            ptdf matrix, nodal or zonal depending of *gsk* argument, containing
            all lines under outages with significant impact.
        b : np.ndarray
            Line capacities of the cbco in A.
        info : pd.DataFrame
            DataFrame containing the ptdf, ram and information which cbco each
            row corresponds to.

        """
        A, b, info = self.grid.create_filtered_n_1_ptdf(sensitivity=sensitivity)
        # Processing: Rounding, remove duplicates and 0...0 rows
        if preprocess:
            self.logger.info("Preprocessing Ab...")
            _, idx = np.unique(info[list(self.grid.nodes.index) + ["ram"]].round(decimals=6).values,
                               axis=0, return_index=True)
            A = A[np.sort(idx)]
            b = b[np.sort(idx)]
            info = info.loc[np.sort(idx), :]

        if gsk:  # replace nodal ptdf by zonal ptdf
            A = np.dot(A, gsk)
            info = pd.concat((info.loc[:, ["cb", "co", "ram"]],
                              pd.DataFrame(columns=self.data.zones.index,
                                           data=A)), axis=1)
        return A, b, info

    def write_cbco_info(self, folder, suffix, **kwargs):
        """Write cbco information to disk to run the redundancy removal algorithm.

        Parameters
        ----------
        folder : pathlib.Path
            Save file to the specified folder.
        suffix : str
            A suffix for each file, to make it recognizable.
        """
        self.logger.info("Saving A, b...")
        
        for data in [d for d in ["x_bounds", "I"] if d not in kwargs]:
            kwargs[data] = np.array([])

        for data in kwargs:
            self.logger.info("Saving %s to disk...", data)
            if isinstance(kwargs[data], np.ndarray):
                np.savetxt(folder.joinpath(f"{data}_{suffix}.csv"),
                           np.asarray(kwargs[data]), delimiter=",")

            elif isinstance(kwargs[data], pd.DataFrame):
                kwargs[data].to_csv(str(folder.joinpath(f'{data}.csv')), index_label='index')

        self.logger.info("Saved everything to folder: \n %s", str(folder))

    def create_nodal_injection_limits(self):
        """Create nodal injection limits.

        For each node the nodal injection limits depend on the installed
        capacity and availability/load timeseries. Additionally, each node can
        have a slack variables/infeasibility variables, DC-line injections and
        storage charge/discharge.

        Because a nodal injection can impact a line positively/negatively
        depending on the (arbitrary) definition of the incidence matrix,
        only the max(positive bound, abs(negative bound)) is considered.


        Returns
        -------
        nodal_injection_limits : np.ndarray
            Contains the abs maximum power injected/load at each node.

        """
        infeasibility_upperbound = self.options["optimization"]["infeasibility"]["electricity"]["bound"]
        nodal_injection_limits = []

        for node in self.data.nodes.index:
            plant_types = self.options["optimization"]["plant_types"]
            condition_storage = (self.data.plants.node == node) & \
                                (self.data.plants.plant_type.isin(plant_types["es"]))
            condition_el_heat = (self.data.plants.node == node) & \
                                (self.data.plants.plant_type.isin(plant_types["ph"]))

            max_dc_inj = self.data.dclines.maxflow[(self.data.dclines.node_i == node) |
                                                   (self.data.dclines.node_j == node)].sum()
            nex_max = max(0, self.data.net_export.loc[self.data.net_export.node == node, "net_export"].max())
            nex_min = -min(0, self.data.net_export.loc[self.data.net_export.node == node, "net_export"].min())
            upper = max(self.data.plants.g_max[self.data.plants.node == node].sum()
                        - self.data.demand_el.loc[self.data.demand_el.node == node, "demand_el"].min()
                        + nex_max
                        + max_dc_inj
                        + infeasibility_upperbound
                        , 0)

            lower = max(self.data.demand_el.loc[self.data.demand_el.node == node, "demand_el"].max()
                        + self.data.plants.g_max[condition_storage].sum()
                        + self.data.plants.g_max[condition_el_heat].sum()
                        + nex_min
                        + max_dc_inj
                        + infeasibility_upperbound, 0)

            nodal_injection_limits.append(max(upper, lower))

        nodal_injection_limits = np.array(nodal_injection_limits).reshape(len(nodal_injection_limits), 1)
        return nodal_injection_limits

    def clarkson_algorithm(self, args={"file_suffix": "py"}, **kwargs):
        """Run the redundancy removal algorithm.

        The redundancy removal algorithm is run by writing the necessary data
        to disk with "_py" suffix, starting a julia instance and running the
        algorithm. After (successful) completion the resulting file with the
        non-redundant cbco indices is read and returned.

        Returns
        -------
        cbco : list
            List of the essential indices, i.e. the indices of the non-redundant
            cbco's.
        """

        self.write_cbco_info(self.julia_dir.joinpath("cbco_data"), "py", **kwargs)

        if not self.julia_instance:
            self.julia_instance = tools.JuliaDaemon(self.logger, self.wdir, self.package_dir, "redundancy_removal")
        if not self.julia_instance.is_alive:
            self.julia_instance = tools.JuliaDaemon(self.logger, self.wdir, self.package_dir, "redundancy_removal")

        t_start = dt.datetime.now()
        self.logger.info("Start-Time: %s", t_start.strftime("%H:%M:%S"))

        self.julia_instance.run(args=args)

        t_end = dt.datetime.now()
        self.logger.info("End-Time: %s", t_end.strftime("%H:%M:%S"))
        self.logger.info("Total Time: %s", str((t_end-t_start).total_seconds()) + " sec")

        if self.julia_instance.solved:
            file = tools.newest_file_folder(self.julia_dir.joinpath("cbco_data"), keyword="cbco")
            self.logger.info("cbco list save for later use to: \n%s", file.stem + ".csv")
            cbco = list(pd.read_csv(file, delimiter=',').constraints.values)
        else:
            self.logger.critical("Error in Julia code")
            cbco = None
        return cbco

    def return_cbco(self, cbco_info, cbco_index):
        """Return only the cbco's of the info attribute DataFrame.

        Returns
        -------
        cbco_info : DataFrame
            Slice of the full info attribute, containing filtered contingency ptdfs,
            based on the cbco indices resulting from the redundancy removal algorithm.

        """
        cbco_info = cbco_info.iloc[cbco_index].copy()
        cbco_info.loc[:, "index"] = cbco_info.cb + "_" + cbco_info.co
        cbco_info = cbco_info.set_index("index")
        return cbco_info

    def create_gsk(self, option="flat"):
        """Create generation shift key (gsk).

        The gsk represents a node to zone mapping or the assumption on how nodal injections
        within a zone are distributed if you only know the zonal net position.

        Based on the argument this method creates a gsk either *flat*, all nodes weighted
        equally or *gmax* with nodes weighted according to the installed conventional capacity.

        Parameters
        ----------
        option : str, optional
            Deciding how nodal injections are weighted. Currently *flat* or *gmax*.

        Returns
        -------
        gsk : np.ndarrays
            gsk in the form of a NxZ matrix (Nodes, Zones). With each column representing
            the weighting of nodes within a zone. The product ptdf * gsk yields the zonal
            ptdf matrix.

        """
        self.logger.info("Creating gsk with option: %s", option)
        gsk = pd.DataFrame(index=self.data.nodes.index)
        condition = (self.data.plants.plant_type.isin(self.options["optimization"]["plant_types"]["ts"]) 
                        & (~self.data.plants.plant_type.isin(self.options["optimization"]["plant_types"]["es"])))
        gmax_per_node = self.data.plants.loc[condition, ["g_max", "node"]].groupby("node").sum()

        for zone in self.data.zones.index:
            nodes_in_zone = self.data.nodes.index[self.data.nodes.zone == zone]
            gsk[zone] = 0
            gmax_in_zone = gmax_per_node[gmax_per_node.index.isin(nodes_in_zone)]
            if option == "gmax":
                if not gmax_in_zone.empty:
                    gsk_value = gmax_in_zone.g_max/gmax_in_zone.values.sum()
                    gsk.loc[gsk.index.isin(gmax_in_zone.index), zone] = gsk_value

            elif option == "flat":
                # if not gmax_in_zone.empty:
                gsk.loc[gsk.index.isin(nodes_in_zone), zone] = 1/len(nodes_in_zone)

        return gsk.values

    def process_ntc(self):
        """Process grid information for NTC representation.

        This only includes assigning ntc data. However if no data is available, dummy
        data is generated.
        """
        if self.data.ntc.empty:
            self.create_ntc()
        else:
            self.grid_representation.ntc = self.data.ntc

    def create_ntc(self):
        """Create NTC data.

        The ntc generated in this method are high (10.000) or zero. This is useful
        to limit commercial exchange to connected zones or when the model uses a
        simplified line representation.

        """
        tmp = []
        for from_zone, to_zone in itertools.combinations(set(self.data.nodes.zone), 2):
            lines = []

            from_nodes = self.data.nodes.index[self.data.nodes.zone == from_zone]
            to_nodes = self.data.nodes.index[self.data.nodes.zone == to_zone]

            condition_i_from = self.data.lines.node_i.isin(from_nodes)
            condition_j_to = self.data.lines.node_j.isin(to_nodes)

            condition_i_to = self.data.lines.node_i.isin(to_nodes)
            condition_j_from = self.data.lines.node_j.isin(from_nodes)

            lines += list(self.data.lines.index[condition_i_from & condition_j_to])
            lines += list(self.data.lines.index[condition_i_to & condition_j_from])

            dclines = []
            condition_i_from = self.data.dclines.node_i.isin(from_nodes)
            condition_j_to = self.data.dclines.node_j.isin(to_nodes)

            condition_i_to = self.data.dclines.node_i.isin(to_nodes)
            condition_j_from = self.data.dclines.node_j.isin(from_nodes)

            dclines += list(self.data.dclines.index[condition_i_from & condition_j_to])
            dclines += list(self.data.dclines.index[condition_i_to & condition_j_from])

            if lines or dclines:
                tmp.append([from_zone, to_zone, 1e5])
                tmp.append([to_zone, from_zone, 1e5])
            else:
                tmp.append([from_zone, to_zone, 0])
                tmp.append([to_zone, from_zone, 0])

        self.grid_representation.ntc = pd.DataFrame(tmp, columns=["zone_i", "zone_j", "ntc"])
